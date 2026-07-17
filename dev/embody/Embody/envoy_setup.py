"""Envoy environment/config/registry setup (module DAT).

Module DAT (mod.envoy_setup) called by EnvoyExt on the MAIN THREAD only
(the ext-diet WP5 cluster). Holds the config-file / git / instance-registry
setup implementations: MCP client config (.mcp.json + STDIO bridge),
settings.local.json tool-permission deployment, git root discovery + repo
init, .gitignore / .gitattributes / .tdn-diff-driver configuration, the
.embody/envoy.json instance registry (write / refresh / deregister, the
post-save basename walk), PID liveness, atomic JSON writes, and temp-file
cleanup. EnvoyExt keeps a thin delegating stub for each -- these functions
carry the real bodies.

MAIN-THREAD ONLY: every function here reads/writes TD objects (ownerComp
params, templates DAT, project.folder, op.Embody.ext.Embody...) and must
never run on a worker thread. The background dependency installer and the
MCP server worker (EnvoyExt._runServer / the _beginAsync* worker closures)
deliberately do NOT call into this module -- they stay on the facade. No
module-level TD access; every function takes the ext instance (`ext`) as
its first argument (except the two former @staticmethods, which stay pure).
"""

# Dispatch contract: intra-module calls are module-local EXCEPT the three
# patchable seams (_isPidAlive / _registryPath / _atomicWriteJSON), which
# route via ext.* so instance monkeypatches (unit tests) keep working. If a
# future test needs to patch another name mid-chain (e.g. _configureGitignore
# under _checkOrInitGitRepo), reroute that call through ext.* the same way.

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time


def configure_mcp_client(ext, port, target_dir=None):
    """Auto-configure MCP client by writing .mcp.json and the STDIO bridge
    script.  Uses STDIO transport so Claude Code always has tools available
    (the bridge retries until Envoy is reachable).
    Idempotent -- safe to call on every start.

    Args:
        port: The port Envoy is running on.
        target_dir: Directory to write config files into. Defaults to
            git root if available, else project.folder.
    """
    from pathlib import Path
    try:
        project_dir = Path(project.folder)

        if target_dir is None:
            # Find the git root by walking up from the .toe directory
            for parent_path in [project_dir] + list(project_dir.parents):
                if (parent_path / '.git').exists():
                    target_dir = parent_path
                    break
            if target_dir is None:
                target_dir = project_dir

        target_dir = Path(target_dir)

        # --- Deploy the STDIO bridge script ---
        bridge_dir = target_dir / '.embody'
        bridge_dir.mkdir(parents=True, exist_ok=True)
        bridge_path = bridge_dir / 'envoy-bridge.py'

        # Read bridge script from templates textDAT, else from disk fallback
        bridge_content = None
        try:
            templates = ext.ownerComp.op('templates')
            bridge_dat = templates.op('text_envoy_bridge') if templates else None
            if bridge_dat:
                bridge_content = bridge_dat.text
        except Exception:
            pass

        if not bridge_content:
            # Fallback: read from the externalized file in dev/embody/
            source = Path(project.folder) / 'embody' / 'envoy_bridge.py'
            if source.exists():
                bridge_content = source.read_text(encoding='utf-8')

        if not bridge_content:
            ext._log(
                'Bridge script source not found -- falling back to HTTP transport',
                'WARNING')
            configure_mcp_client_http(ext, target_dir, port)
            return

        # Write bridge script only if content changed -- preserving
        # mtime prevents Claude Code's file watcher from restarting
        # the MCP server mid-connection.
        needs_write = True
        if bridge_path.exists():
            try:
                existing = bridge_path.read_text(encoding='utf-8')
                if existing == bridge_content:
                    needs_write = False
            except OSError:
                pass  # Can't read -- overwrite

        if needs_write:
            bridge_path.write_text(bridge_content, encoding='utf-8')
            if sys.platform != 'win32':
                bridge_path.chmod(0o755)
        else:
            if sys.platform != 'win32':
                bridge_path.chmod(0o755)

        # Migrate: remove old bridge from .claude/ if it exists
        old_bridge = target_dir / '.claude' / 'envoy-bridge.py'
        if old_bridge.exists():
            try:
                old_bridge.unlink()
                ext._log('Migrated: removed old .claude/envoy-bridge.py')
            except OSError:
                pass

        # Migrate: remove old files from previous locations
        for old_name, desc in [('.envoy-tools-cache.json', 'tools cache'),
                                ('.envoy.json', 'envoy config'),
                                ('.embody.json', 'embody config')]:
            old_file = target_dir / old_name
            if old_file.exists():
                try:
                    old_file.unlink()
                    ext._log(f'Migrated: removed old {old_name} ({desc})')
                except OSError:
                    pass

        # Prefer the venv Python (created from TD's Python) so the bridge
        # works on machines without a system Python installation.
        # Fall back to system PATH command if the venv doesn't exist yet.
        if sys.platform == 'win32':
            venv_python = project_dir / '.venv' / 'Scripts' / 'python.exe'
        else:
            venv_python = project_dir / '.venv' / 'bin' / 'python3'

        # Windows: keep the probe's console window from flashing over
        # TD's GUI (subprocess of a GUI process opens a console briefly).
        _probe_flags = (subprocess.CREATE_NO_WINDOW
                        if sys.platform == 'win32' else 0)

        if (venv_python.is_file()
                and getattr(ext, '_venv_probe_ok', '') == str(venv_python)):
            # THIS venv's python already probed successfully this
            # extension session (keyed by path -- a mid-session project
            # root switch must re-probe the other venv). Start() re-runs
            # on every watchdog revive, and re-running the synchronous
            # subprocess probe each time was a recurring main-thread
            # stall (issue #60). NOTE: the in-process import gate
            # (sys._envoy_import_gate_ok) is NOT a substitute for the
            # probe -- it validates the venv's packages from TD's own
            # interpreter, not the venv python binary itself.
            python_cmd = str(venv_python).replace('\\', '/')
        elif venv_python.is_file():
            # Verify the venv Python actually executes -- catches stale
            # pyvenv.cfg pointing to an uninstalled TD version, or
            # code-signing mismatches after macOS TD upgrades.
            # stdin=DEVNULL: without it, subprocess.run inside TD on
            # Windows raises [WinError 50] (DuplicateHandle on TD's
            # non-duplicatable GUI stdin handle) -- which then triggers
            # the rmtree path below and destroys a healthy venv.
            try:
                subprocess.run(
                    [str(venv_python), '-c',
                     'import sys; print(sys.version)'],
                    capture_output=True, timeout=5, check=True,
                    stdin=subprocess.DEVNULL, creationflags=_probe_flags)
                python_cmd = str(venv_python).replace('\\', '/')
                ext._venv_probe_ok = str(venv_python)
            except subprocess.TimeoutExpired:
                # Slow is not corrupt: a cold disk or AV scan can stall a
                # healthy interpreter past the timeout. Never rmtree a
                # venv for being slow -- fall back to system Python for
                # this config write and let a later Start() re-probe.
                ext._log(
                    'Venv Python probe timed out; using system Python '
                    'for now (will re-probe on next start)', 'WARNING')
                python_cmd = ('python' if sys.platform == 'win32'
                              else 'python3')
            except (subprocess.CalledProcessError, OSError) as e:
                if not ext._venv_recreated:
                    ext._venv_recreated = True
                    ext._log(
                        f'Venv corrupted ({type(e).__name__}: {e}), '
                        f'recreating...', 'WARNING')
                    import shutil
                    shutil.rmtree(str(project_dir / '.venv'),
                                  ignore_errors=True)
                    op.Embody.ext.Embody._setupEnvironment()
                    # Re-check after recreation
                    if venv_python.is_file():
                        try:
                            subprocess.run(
                                [str(venv_python), '-c',
                                 'import sys; print(sys.version)'],
                                capture_output=True, timeout=5,
                                check=True,
                                stdin=subprocess.DEVNULL,
                                creationflags=_probe_flags)
                            python_cmd = str(venv_python).replace(
                                '\\', '/')
                            ext._venv_probe_ok = str(venv_python)
                            ext._log('Venv recreated successfully',
                                     'SUCCESS')
                        except Exception as e2:
                            ext._log(
                                f'Venv recreation failed: {e2}. '
                                f'Using system Python.', 'ERROR')
                            python_cmd = ('python' if sys.platform == 'win32'
                                          else 'python3')
                    else:
                        ext._log(
                            'Venv recreation did not produce Python '
                            'binary. Using system Python.', 'ERROR')
                        python_cmd = ('python' if sys.platform == 'win32'
                                      else 'python3')
                else:
                    ext._log(
                        f'Venv Python still broken after recreation: '
                        f'{e}. Using system Python.', 'WARNING')
                    python_cmd = ('python' if sys.platform == 'win32'
                                  else 'python3')
        else:
            python_cmd = 'python' if sys.platform == 'win32' else 'python3'

        # --- Deploy the .tdn git diff driver (semantic git diffs) ---
        configure_tdn_diff_driver(ext, target_dir, python_cmd)

        # --- Write envoy.json project config ---
        write_envoy_config(ext, target_dir / '.embody', port)

        # --- Write .mcp.json with STDIO transport ---
        mcp_file = target_dir / '.mcp.json'
        # Use forward slashes even on Windows for JSON portability
        bridge_abs = str(bridge_path).replace('\\', '/')
        config_abs = str(
            (target_dir / '.embody' / 'envoy.json')).replace('\\', '/')

        # Record .mcp.json footprint: Embody manages the mcpServers.envoy
        # key. If it created the file, Uninstall may delete it; if it merged
        # into a pre-existing one, Uninstall removes only that key.
        try:
            Embody = op.Embody.ext.Embody
            if mcp_file.exists():
                Embody._manifestRecordAppendedFile(
                    str(target_dir), mcp_file, 'mcpServers.envoy',
                    kind='json_key')
            else:
                Embody._manifestRecordCreatedFile(str(target_dir), mcp_file)
        except Exception:
            pass

        # Read existing config to preserve other servers
        config = {}
        if mcp_file.exists():
            try:
                config = json.loads(mcp_file.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError) as e:
                ext._log(f'Could not parse existing .mcp.json, will overwrite: {e}', 'DEBUG')

        servers = config.get('mcpServers', {})
        existing = servers.get('envoy', {})

        # Check if already configured with matching STDIO bridge
        expected_args = ['-u', bridge_abs, '--port', str(port),
                         '--config', config_abs]
        if (existing.get('type') == 'stdio'
                and existing.get('command') == python_cmd
                and existing.get('args') == expected_args):
            ext._log('MCP .mcp.json already configured (STDIO bridge)', 'DEBUG')
            deploy_settings_local(ext, target_dir / '.claude')
            return

        servers['envoy'] = {
            'type': 'stdio',
            'command': python_cmd,
            'args': expected_args,
        }
        config['mcpServers'] = servers

        def _write():
            mcp_file.write_text(
                json.dumps(config, indent=2) + '\n', encoding='utf-8')
            ext._log(f'Wrote MCP config to {mcp_file} (STDIO bridge -> port {port})')

        # Advanced mode: confirm before writing the Envoy entry into the
        # user's .mcp.json (only reached when it is missing or out of date).
        verb = 'add the Envoy MCP server entry to' if existing else 'create'
        op.Embody.ext.Embody._guardFileWrite(
            'MCP config', f'{verb} .mcp.json in {target_dir}',
            [str(mcp_file)], _write)

        # --- Deploy settings.local.json (auto-allow read-only MCP tools) ---
        deploy_settings_local(ext, target_dir / '.claude')

    except Exception as e:
        ext._log(f'Could not auto-configure MCP client: {e}', 'WARNING')


def configure_mcp_client_http(ext, target_dir, port):
    """Fallback: configure .mcp.json with direct HTTP transport.
    Used when the STDIO bridge script cannot be deployed."""
    url = f'http://localhost:{port}/mcp'
    mcp_file = target_dir / '.mcp.json'

    config = {}
    if mcp_file.exists():
        try:
            config = json.loads(mcp_file.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass

    servers = config.get('mcpServers', {})
    servers['envoy'] = {'type': 'http', 'url': url}
    config['mcpServers'] = servers
    mcp_file.write_text(
        json.dumps(config, indent=2) + '\n', encoding='utf-8')
    ext._log(f'Wrote MCP config to {mcp_file} (HTTP fallback)')


def registry_path(ext):
    """Path to .embody/envoy.json honoring Aiprojectroot.

    All registry I/O (port-conflict detection, RefreshRegistry,
    deregistration) must go through here -- the registry must live
    co-located with .mcp.json, which itself follows Aiprojectroot
    via _findProjectRoot. Defaults to legacy git_root behavior if
    the Embody extension isn't accessible (defensive).
    """
    from pathlib import Path
    try:
        root = op.Embody.ext.Embody._findProjectRoot()
        return Path(root) / '.embody' / 'envoy.json'
    except Exception:
        git_root = ext.ownerComp.fetch('_git_root', 'no-git')
        if git_root == 'no-git':
            return None
        return Path(git_root) / '.embody' / 'envoy.json'


def tool_permissions_posture(ext):
    """The Toolpermissions param value, defensively normalized.
    'all' | 'some' | 'prompt' | 'leave' (default 'all')."""
    try:
        posture = (op.Embody.par.Toolpermissions.eval() or 'all').strip().lower()
    except Exception:
        posture = 'all'
    return posture if posture in ('all', 'some', 'prompt', 'leave') else 'all'


def temp_read_dirs(ext):
    """Directories that must be readable so a capture_top PNG (saved to the
    OS temp dir, EnvoyExt._captureTop) can be Read without a prompt. Forward
    slashes for cross-platform JSON. Always includes /tmp as a fallback."""
    dirs = ['/tmp']
    try:
        t = tempfile.gettempdir().replace('\\', '/')
        if t and t not in dirs:
            dirs.append(t)
    except Exception:
        pass
    return dirs


def load_settings_baseline(ext):
    """The NON-Envoy baseline settings dict (from the template DAT if
    present, else a built-in minimum). The Envoy allow entries + temp read
    dirs are layered on per posture by _composeSettings, so the template no
    longer needs to enumerate them."""
    try:
        templates = ext.ownerComp.op('templates')
        dat = templates.op('text_settings_local') if templates else None
        if dat and (dat.text or '').strip():
            cfg = json.loads(dat.text)
            if isinstance(cfg, dict):
                return cfg
    except Exception as e:
        ext._log(f'settings baseline template unreadable ({e}); '
                 f'using built-in.', 'DEBUG')
    return {
        'permissions': {
            'allow': ['Bash', 'WebFetch'],
            'additionalDirectories': ['/tmp'],
        },
        'enabledMcpjsonServers': ['envoy'],
        'enableAllProjectMcpServers': True,
    }


def compose_settings(ext, cfg, posture):
    """Apply a tool-permissions posture onto a settings dict IN PLACE and
    return it. Preserves every non-Envoy key and every non-Envoy allow
    entry; replaces only the Envoy tool entries, ensures the temp read
    dirs, and trusts the Envoy MCP server. `posture` is never 'leave'
    here (the caller short-circuits that)."""
    perms = cfg.setdefault('permissions', {})
    # Strip prior Envoy entries so we author them fresh for this posture.
    allow = [a for a in perms.get('allow', [])
             if not (a == 'mcp__envoy' or a.startswith('mcp__envoy__'))]
    if posture == 'all':
        allow.append('mcp__envoy')          # wildcard: all current + future tools
    elif posture == 'some':
        allow.extend(f'mcp__envoy__{t}' for t in ext.READ_ONLY_TOOLS)
    # posture == 'prompt': no Envoy entries -> every tool prompts.
    perms['allow'] = allow
    add = list(perms.get('additionalDirectories', []))
    for d in temp_read_dirs(ext):
        if d not in add:
            add.append(d)
    perms['additionalDirectories'] = add
    cfg['permissions'] = perms
    # Trust the project MCP server so tools are available at all.
    cfg['enableAllProjectMcpServers'] = True
    servers = list(cfg.get('enabledMcpjsonServers', []))
    if 'envoy' not in servers:
        servers.append('envoy')
    cfg['enabledMcpjsonServers'] = servers
    return cfg


def settings_satisfies(ext, cfg, posture):
    """True if an existing settings dict already matches `posture` (so no
    rewrite is needed). Semantic, order-insensitive -- avoids startup churn
    from list reordering."""
    if not isinstance(cfg, dict):
        return False
    perms = cfg.get('permissions', {}) or {}
    allow = perms.get('allow', []) or []
    envoy = [a for a in allow
             if a == 'mcp__envoy' or a.startswith('mcp__envoy__')]
    add = perms.get('additionalDirectories', []) or []
    temp_ok = all(d in add for d in temp_read_dirs(ext))
    server_ok = (cfg.get('enableAllProjectMcpServers') is True
                 or 'envoy' in (cfg.get('enabledMcpjsonServers') or []))
    if not (temp_ok and server_ok):
        return False
    if posture == 'all':
        return 'mcp__envoy' in envoy
    if posture == 'prompt':
        return len(envoy) == 0
    if posture == 'some':
        return set(envoy) == {f'mcp__envoy__{t}' for t in ext.READ_ONLY_TOOLS}
    return False


def deploy_settings_local(ext, claude_dir):
    """Write .claude/settings.local.json to match the Toolpermissions
    posture, so Claude Code isn't prompted on every Envoy MCP tool call
    (and so a captured TOP in the OS temp dir can be Read without a prompt).

    Postures: all = auto-approve all Envoy tools (wildcard); some =
    read-only tools only; prompt = none; leave = don't touch the file.
    Merges into an existing file, preserving every non-Envoy key, and is
    idempotent (skips when the posture is already satisfied -- no churn).
    The user is told whether the file was created or updated.
    """
    import copy
    posture = tool_permissions_posture(ext)
    settings_path = claude_dir / 'settings.local.json'

    if posture == 'leave':
        ext._log('Tool permissions: leaving .claude/settings.local.json '
                 'untouched (your choice).', 'DEBUG')
        return

    existed = settings_path.exists()
    existing = None
    if existed:
        try:
            existing = json.loads(settings_path.read_text(encoding='utf-8'))
            if not isinstance(existing, dict):
                existing = None
        except (json.JSONDecodeError, OSError) as e:
            # Never clobber a settings.local.json we can't parse -- it may
            # hold hand-authored user permissions.
            ext._log(f'Could not parse existing settings.local.json '
                     f'({e}) -- leaving it untouched.', 'WARNING')
            return

    # Idempotent: an already-satisfying file is left exactly as-is.
    if existing is not None and settings_satisfies(ext, existing, posture):
        ext._log(f'settings.local.json already matches tool permissions '
                 f'({posture}) -- no change.', 'DEBUG')
        return

    base = copy.deepcopy(existing) if existing is not None \
        else load_settings_baseline(ext)
    new_cfg = compose_settings(ext, base, posture)
    content = json.dumps(new_cfg, indent=2) + '\n'
    verb = 'update' if existed else 'create'

    def _write():
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(content, encoding='utf-8')
        ext._log(f'{verb.capitalize()}d .claude/settings.local.json '
                 f'(tool permissions: {posture}) at {settings_path}',
                 'SUCCESS')
        try:
            Embody = op.Embody.ext.Embody
            root = str(Embody._findProjectRoot())
            if existed:  # merged into a user file -> Uninstall only reverses our unit
                Embody._manifestRecordAppendedFile(
                    root, settings_path,
                    'permissions (Envoy tools + temp read dirs)',
                    kind='json_key')
            else:        # Embody created it -> safe to remove on Uninstall
                Embody._manifestRecordCreatedFile(root, settings_path)
        except Exception:
            pass

    # Advanced mode confirms; Auto / consented-batch apply silently. The
    # 'update' verb makes the disclosure honest about touching a user file.
    op.Embody.ext.Embody._guardFileWrite(
        'AI config',
        f'{verb} .claude/settings.local.json (tool permissions: {posture}) '
        f'in {claude_dir.parent}',
        [str(settings_path)],
        _write)


def find_git_root(ext):
    """Silently find the git repo root. Returns Path or 'no-git'. Never prompts."""
    from pathlib import Path
    project_dir = Path(project.folder).resolve()
    try:
        home_dir = Path.home().resolve()
    except Exception:
        home_dir = None
    # Only stop at home_dir when it's actually an ancestor of project_dir.
    # Otherwise (e.g. Windows project on D:\ while home is on C:\) the
    # part-count comparison wrongly bailed before searching -- issue #19.
    home_is_ancestor = bool(
        home_dir and (home_dir == project_dir or home_dir in project_dir.parents)
    )
    for parent in [project_dir] + list(project_dir.parents):
        if home_is_ancestor and parent == home_dir:
            break
        if (parent / '.git').exists():
            ext._log(f'Found git repo at {parent}', 'INFO')
            return parent
    ext._log(f'No git repo found for {project_dir}', 'INFO')
    return 'no-git'


def check_or_init_git_repo(ext):
    """Check for a git repo. If missing, prompt user to initialize one.
    Only call from user-initiated flows (_enableEnvoy, InitGit) -- never
    from automatic startup paths. Returns Path, 'no-git', or None (cancelled)."""
    from pathlib import Path
    import os, subprocess

    project_dir = Path(project.folder).resolve()
    try:
        home_dir = Path.home().resolve()
    except Exception:
        home_dir = None

    # Walk up looking for .git, but stop at the home directory only when
    # home is actually an ancestor of project_dir (issue #19 -- previously
    # the comparison broke for projects on a non-home drive on Windows).
    home_is_ancestor = bool(
        home_dir and (home_dir == project_dir or home_dir in project_dir.parents)
    )
    for parent in [project_dir] + list(project_dir.parents):
        if home_is_ancestor and parent == home_dir:
            break
        if (parent / '.git').exists():
            ext._log(f'Found git repo at {parent}', 'INFO')
            return parent

    # No git repo found between project folder and home directory.
    ext._log(
        f'No git repo found for {project_dir} (stopped at {home_dir})',
        'INFO')

    # Prompt user.
    # Guard against concurrent calls: ui.messageBox blocks the main
    # thread but TD's run() callbacks still fire, so a second Start()
    # can reach here while the first dialog is open.
    if getattr(ext, '_git_prompt_active', False):
        ext._log('Git prompt already active (duplicate suppressed)', 'DEBUG')
        return 'no-git'
    ext._git_prompt_active = True
    try:
        choice = op.Embody.ext.Embody._messageBox(
            'Envoy -- Git Repository Recommended',
            'A git repository is recommended for .gitignore and\n'
            '.gitattributes management. No git repository was found.\n\n'
            'MCP and AI client config files will be generated either way.\n\n'
            f'Initialize a git repo in:\n  {project_dir}\n\n'
            'Or browse to select a different folder (e.g. an existing repo root).\n'
            'You can also run op.Embody.InitGit() later.',
            buttons=['Cancel', 'Initialize Git Here', 'Browse for Folder', 'Start Without Git'])

        if choice not in (1, 2, 3):  # Cancel or closed dialog
            ext.ownerComp.par.Envoyenable = False
            ext._log('Envoy cancelled -- no git repository.', 'INFO')
            return None

        if choice == 2:  # Browse for Folder
            result = ui.chooseFolder(
                title='Select Git Repository Root', start=str(project_dir))
            if not result:
                ext.ownerComp.par.Envoyenable = False
                ext._log('Envoy cancelled -- folder selection aborted.', 'INFO')
                return None
            chosen = Path(result)
            # If the chosen folder already contains a .git, use it directly
            if (chosen / '.git').exists():
                ext._log(f'Using existing git repo at {chosen}', 'SUCCESS')
                return chosen
            # No .git there -- offer to initialize in that folder
            init_choice = op.Embody.ext.Embody._messageBox(
                'Envoy -- Initialize Git',
                f'No git repo found in:\n  {chosen}\n\nInitialize git here?',
                buttons=['Cancel', 'Initialize Git'])
            if init_choice not in (1,):
                ext.ownerComp.par.Envoyenable = False
                return None
            project_dir = chosen  # use chosen folder for init below

        if choice in (1, 2):  # Initialize Git Here, or Browse -> confirmed init
            try:
                # Strip git env vars that TD's embedded Python may set --
                # these can cause git init to produce a broken repository.
                clean_env = {
                    k: v for k, v in os.environ.items()
                    if k not in (
                        'GIT_DIR', 'GIT_WORK_TREE',
                        'GIT_INDEX_FILE', 'GIT_CEILING_DIRECTORIES',
                    )
                }
                git_kwargs = dict(
                    capture_output=True, text=True,
                    cwd=str(project_dir), env=clean_env,
                )
                subprocess.run(['git', 'init'], check=True, **git_kwargs)
                ext._log(f'Initialized git repo in {project_dir}', 'SUCCESS')

                # Verify the init produced a working repository
                verify = subprocess.run(
                    ['git', 'rev-parse', '--is-inside-work-tree'],
                    **git_kwargs)
                if verify.returncode != 0:
                    ext._log('Git verify failed after init -- retrying', 'WARNING')
                    subprocess.run(['git', 'init'], check=True, **git_kwargs)
                    verify = subprocess.run(
                        ['git', 'rev-parse', '--is-inside-work-tree'],
                        **git_kwargs)
                    if verify.returncode != 0:
                        raise RuntimeError(
                            f'git rev-parse failed after retry: '
                            f'{verify.stderr.strip()}')
                    ext._log('Git repo verified after retry', 'SUCCESS')

                # Git config files belong with git init (issue #8).
                configure_gitignore(ext, project_dir)
                configure_gitattributes(ext, project_dir)

                return project_dir
            except Exception as e:
                ext._log(f'Failed to initialize git repo: {e}', 'ERROR')
                op.Embody.ext.Embody._messageBox(
                    'Envoy -- Git Initialization Failed',
                    f'Could not initialize a git repository:\n\n  {e}\n\n'
                    'Envoy will start without git. MCP and AI client\n'
                    'config will be generated in the project folder.\n'
                    '.gitignore and .gitattributes will be skipped.\n\n'
                    'To add git later: run "git init" manually, then\n'
                    'call op.Embody.InitGit() from the textport.',
                    buttons=['OK'])
                # Fall through to start-without-git

        # choice == 3 or git init failed -- start without git
        ext._log('Starting Envoy without git repo -- auto-config skipped.', 'WARNING')
        return 'no-git'
    finally:
        ext._git_prompt_active = False


def atomic_write_json(path, data):
    """Write JSON atomically via temp file + os.replace().
    Retries on PermissionError (Windows file-in-use)."""
    import os
    from pathlib import Path
    tmp = Path(str(path) + '.tmp')
    content = json.dumps(data, indent=2) + '\n'
    for attempt in range(3):
        try:
            tmp.write_text(content, encoding='utf-8')
            os.replace(str(tmp), str(path))
            return
        except PermissionError:
            if attempt < 2:
                import time as _time
                _time.sleep(0.1)
            else:
                raise


def instance_key(ext, toe_rel: str, existing_instances: dict) -> str:
    """Compute a unique instance key from the toe filename.
    Uses basename without .toe.  Appends -2, -3, etc. on collision
    with a live instance (same or different toe_path).

    Walks forward across TD's auto-version-bump on save: if this PID
    is already registered and its registered toe_path STILL matches
    the current path, the existing key is reused (no churn). If the
    toe_path has changed (rename, save-as-version-up), a fresh key
    is computed from the new basename and the caller is responsible
    for pruning the stale entry under the old key.

    If Envoyinstancename is set, uses that as the key instead."""
    import os
    from pathlib import Path

    # User override via parameter
    try:
        custom = ext.ownerComp.par.Envoyinstancename.eval()
        if custom:
            return custom
    except:
        pass

    base = Path(toe_rel).stem  # e.g. 'Embody-5.251'
    my_pid = os.getpid()

    # Re-registration with same toe_path: keep the existing key.
    # If the toe_path has changed, fall through and compute a new
    # key from the current basename -- caller prunes the stale row.
    for key, info in existing_instances.items():
        if (info.get('td_pid') == my_pid
                and info.get('toe_path') == toe_rel):
            return key

    # Check if base key is free, held by a dead process, or held
    # by our own previous (now-stale) registration.
    if base not in existing_instances:
        return base
    existing_pid = existing_instances[base].get('td_pid', 0)
    if not ext._isPidAlive(existing_pid) or existing_pid == my_pid:
        return base

    # Base key is held by a live foreign process -- find a unique suffix
    suffix = 2
    while True:
        candidate = f'{base}-{suffix}'
        if candidate not in existing_instances:
            return candidate
        existing_pid = existing_instances[candidate].get('td_pid', 0)
        if not ext._isPidAlive(existing_pid) or existing_pid == my_pid:
            return candidate
        suffix += 1


def is_pid_alive(pid):
    """Check whether a process with the given PID is alive.

    CRITICAL: do NOT use ``os.kill(pid, 0)`` on Windows.  CPython's
    posixmodule implements ``os.kill`` on Windows via
    ``OpenProcess(PROCESS_ALL_ACCESS, ...)`` + ``TerminateProcess(handle, sig)``
    regardless of ``sig`` -- when called with ``sig=0`` on a foreign
    TD process Embody has access to, it would silently terminate that
    process with exit code 0.  And when the PID is invalid in a
    particular way (e.g. registry corruption, a wrapped-around PID,
    a non-int), ``OpenProcess`` returns ``INVALID_HANDLE_VALUE``
    instead of NULL; the subsequent ``TerminateProcess`` fails with
    ``WinError 87`` and CPython's wrapper raises ``OSError`` *while
    leaving the interpreter thread state inconsistent*, surfacing as
    ``SystemError: <class 'OSError'> returned a result with an
    exception set`` and intermittently aborting the process on the
    next interpreter tick.  Mirror the bridge's safe pattern instead.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    # POSIX: signal 0 is a real no-op liveness check.  Catch
    # OverflowError too -- pid_t is int32 on most kernels and a
    # registry that's been corrupted with a giant value would
    # otherwise propagate the overflow up through _writeEnvoyConfig.
    import os
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except (OSError, OverflowError, ValueError):
        return False


def write_envoy_config(ext, embody_dir, port):
    """Register this instance in the .embody/envoy.json instance registry.

    The registry tracks all running Envoy instances so the bridge can
    discover and switch between them.  Atomic writes prevent corruption
    when multiple TD instances write concurrently.

    Format:
        {
            "active": "Embody-5.251",
            "td_executable": "/path/to/TouchDesigner",
            "instances": {
                "Embody-5.251": {
                    "toe_path": "dev/Embody-5.251.toe",
                    "port": 9870,
                    "td_pid": 12345
                }
            }
        }
    """
    import os
    import td as _td
    from pathlib import Path

    embody_dir.mkdir(parents=True, exist_ok=True)
    config_path = embody_dir / 'envoy.json'
    # git_root is embody_dir's parent
    git_root = embody_dir.parent

    # Compute toe_path relative to git root
    project_dir = Path(project.folder)
    name = project.name
    toe_file = project_dir / (name if name.endswith('.toe') else name + '.toe')
    try:
        toe_rel = str(toe_file.relative_to(git_root)).replace('\\', '/')
    except ValueError:
        toe_rel = str(toe_file).replace('\\', '/')

    # Derive TD executable path from app.binFolder
    bin_folder = Path(_td.app.binFolder)
    if sys.platform == 'darwin':
        td_executable = str(bin_folder.parent.parent)
    elif sys.platform == 'win32':
        exe = bin_folder / 'TouchDesigner.exe'
        if not exe.exists():
            exe = bin_folder / 'TouchDesigner099.exe'
        td_executable = str(exe).replace('\\', '/')
    else:
        td_executable = str(bin_folder / 'TouchDesigner')

    # Read existing config (migrate from old root-level .envoy.json)
    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(
                config_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    elif (git_root / '.envoy.json').exists():
        try:
            existing = json.loads(
                (git_root / '.envoy.json').read_text(encoding='utf-8'))
            ext._log('Migrated: seeded envoy.json from old .envoy.json')
        except (json.JSONDecodeError, OSError):
            pass

    # Migrate old flat format -> registry format
    if 'instances' not in existing:
        instances = {}
        if 'toe_path' in existing:
            # Wrap old flat config as a single instance
            old_key = Path(existing['toe_path']).stem
            instances[old_key] = {
                'toe_path': existing.get('toe_path', ''),
                'port': existing.get('port', port),
                'td_pid': existing.get('td_pid', 0),
            }
        existing = {
            'active': existing.get('active', ''),
            'td_executable': existing.get('td_executable', td_executable),
            'instances': instances,
        }

    instances = existing.get('instances', {})
    key = instance_key(ext, toe_rel, instances)
    my_pid = os.getpid()

    # Garbage-collect any registry rows whose PID is no longer
    # alive. Embody only deregisters cleanly on graceful shutdown
    # (Stop()/onDestroyTD); hard kills, force-quits, OS crashes,
    # and Cmd+Q-without-Envoy-stop all leave dead rows behind that
    # accumulate across sessions. Running this on every registry
    # write keeps the file bounded.
    dead_keys = [
        k for k, info in list(instances.items())
        if not ext._isPidAlive(info.get('td_pid', 0))
    ]
    for dead_key in dead_keys:
        del instances[dead_key]
    if dead_keys:
        ext._log(
            f'Pruned {len(dead_keys)} dead registry '
            f'{"row" if len(dead_keys) == 1 else "rows"}: '
            f'{", ".join(repr(k) for k in dead_keys)}', 'DEBUG')

    # Prune stale entries under different keys for the same PID
    # (left over from a prior toe rename, e.g. TD's save-time
    # version bump). Keeps the registry walking forward instead of
    # accumulating dead aliases.
    stale_keys = [
        k for k, info in list(instances.items())
        if info.get('td_pid') == my_pid and k != key
    ]
    for stale_key in stale_keys:
        del instances[stale_key]
        ext._log(
            f'Pruned stale registry key "{stale_key}" '
            f'(PID {my_pid} now registered as "{key}")', 'DEBUG')

    # Build this instance's entry
    new_entry = {
        'toe_path': toe_rel,
        'port': port,
        'td_pid': my_pid,
    }

    # Check if already up-to-date (no stale prune happened either)
    if (not stale_keys
            and instances.get(key) == new_entry
            and existing.get('active') == key):
        ext._log('envoy.json already up to date', 'DEBUG')
        return

    instances[key] = new_entry
    existing['instances'] = instances
    existing['active'] = key
    existing['td_executable'] = td_executable

    ext._atomicWriteJSON(config_path, existing)
    ext._log(f'Registered instance "{key}" in envoy.json (port {port})')


def refresh_registry(ext):
    """Re-register this instance in envoy.json under its current
    toe basename. Safe to call repeatedly (idempotent when nothing
    has changed). Used after `project.save()` to walk the registry
    forward across TD's save-time version bump -- the toe goes
    from `Foo-5.398.toe` to `Foo-5.399.toe` and the registry needs
    to follow.

    Reads the running port from envoy.json by looking up our own
    PID, since EnvoyExt does not retain a runtime port attribute
    (the actual server lives on a worker thread)."""
    import os

    config_path = ext._registryPath()
    if config_path is None or not config_path.exists():
        return

    try:
        existing = json.loads(config_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return

    my_pid = os.getpid()
    port = 0
    for info in existing.get('instances', {}).values():
        if info.get('td_pid') == my_pid:
            port = info.get('port', 0)
            break
    if not port:
        # We aren't in the registry yet (Envoy may have only just
        # started, or not started at all). Nothing to refresh.
        return

    try:
        write_envoy_config(ext, config_path.parent, port)
    except Exception as e:
        ext._log(f'RefreshRegistry failed: {e}', 'WARNING')


def remove_from_registry(ext, git_root=None):
    """Remove this instance from the .embody/envoy.json registry on shutdown.

    Honors Aiprojectroot via _registryPath. The git_root kwarg is kept
    for backward compatibility but only used as a defensive fallback
    when the live registry path isn't resolvable.
    """
    import os
    from pathlib import Path

    config_path = ext._registryPath()
    if config_path is None and git_root is not None and git_root != 'no-git':
        config_path = Path(git_root) / '.embody' / 'envoy.json'
    if config_path is None or not config_path.exists():
        return

    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return

    instances = config.get('instances', {})
    if not instances:
        return

    # Find our entry by PID
    my_pid = os.getpid()
    my_key = None
    for key, info in instances.items():
        if info.get('td_pid') == my_pid:
            my_key = key
            break

    if my_key is None:
        return

    del instances[my_key]
    config['instances'] = instances

    # If we were active, switch to first remaining instance (or null)
    if config.get('active') == my_key:
        remaining = list(instances.keys())
        config['active'] = remaining[0] if remaining else None

    try:
        ext._atomicWriteJSON(config_path, config)
        ext._log(f'Deregistered instance "{my_key}" from envoy.json')
    except Exception as e:
        ext._log(f'Could not deregister from envoy.json: {e}', 'WARNING')


def configure_gitignore(ext, git_root):
    """Ensure .gitignore in the git root contains entries for
    Embody/Envoy auto-generated files.
    Idempotent -- only appends missing entries, preserves all existing content.
    Migrates old `.claude/` blanket entry to specific entries."""
    MANAGED_ENTRIES = [
        # TouchDesigner project
        'Backup/',
        'logs/',
        'CrashAutoSave*',
        # Embody / Envoy
        '.venv/',
        '.mcp.json',
        # Ignore .embody/ runtime files but keep committed project.json
        '.embody/*',
        '!.embody/project.json',
        '.claude/settings.local.json',
        '.claude/projects/',
        '__pycache__/',
        '.DS_Store',
    ]

    try:
        gitignore = git_root / '.gitignore'

        existing_content = ''
        existing_lines = []
        if gitignore.exists():
            existing_content = gitignore.read_text(encoding='utf-8')
            existing_lines = existing_content.splitlines()

        # Migrate: remove stale entries from older Embody versions.
        # NOTE: .envoy-tools-cache.json is intentionally kept gitignored
        # (v5.0.356+) because a root-level cache can still be written
        # by legacy paths; we don't want to accidentally commit it.
        STALE_ENTRIES = {'.claude/', '.claude/envoy-bridge.py',
                         '.envoy.json', '.embody.json',
                         '.embody/envoy-bridge.py',
                         '.embody/envoy-tools-cache.json',
                         # v5.0.387: replaced by '.embody/*' + '!.embody/project.json'
                         # so .embody/project.json (committed td_build pin) is tracked.
                         '.embody/'}
        existing_stripped = {line.strip() for line in existing_lines}
        found_stale = STALE_ENTRIES & existing_stripped
        if found_stale:
            existing_lines = [
                line for line in existing_lines
                if line.strip() not in STALE_ENTRIES
            ]
            existing_content = '\n'.join(existing_lines)
            if existing_content and not existing_content.endswith('\n'):
                existing_content += '\n'
            ext._log(f'Migrated .gitignore: removed stale entries {found_stale}')

        existing_stripped = {line.strip() for line in existing_lines}
        missing = [e for e in MANAGED_ENTRIES if e not in existing_stripped]

        if not missing:
            ext._log('.gitignore already configured', 'DEBUG')
            return

        block = '\n# Embody / Envoy (auto-managed)\n'
        block += '\n'.join(missing) + '\n'

        if existing_content and not existing_content.endswith('\n'):
            block = '\n' + block

        def _write():
            gitignore.write_text(existing_content + block, encoding='utf-8')
            ext._log(f'Added {len(missing)} entries to .gitignore: {", ".join(missing)}')
            try:  # record the marked block so Uninstall strips only it (never the user's file)
                Embody = op.Embody.ext.Embody
                Embody._manifestRecordAppendedFile(
                    str(Embody._findProjectRoot()), gitignore, '# Embody / Envoy')
            except Exception:
                pass

        # Advanced mode: confirm before editing the user's .gitignore. Only
        # reached when entries are actually missing, so a no-op never prompts.
        op.Embody.ext.Embody._guardFileWrite(
            'Git config',
            f'add {len(missing)} entr{"y" if len(missing) == 1 else "ies"} to '
            f'.gitignore in {git_root}',
            list(missing),
            _write)

    except Exception as e:
        ext._log(f'Could not auto-configure .gitignore: {e}', 'WARNING')


def configure_gitattributes(ext, git_root):
    """Ensure .gitattributes normalizes line endings for TD-exported files
    and enables semantic diffs for .tdn. TouchDesigner writes CRLF on all
    platforms; this forces LF in git so externalized files don't show as
    dirty after every TD save. The `diff=tdn` attribute pairs with the git
    diff driver registered by _configureTdnDiffDriver, so `git diff` on a
    .tdn shows only real network changes -- the volatile export header
    (build/timestamp/version/source .toe) is stripped before diffing.
    Idempotent -- migrates an existing managed block that predates the
    diff driver."""
    MANAGED_BLOCK = (
        '\n# Embody / Envoy -- normalize TD line endings (auto-managed)\n'
        '*.py text eol=lf\n'
        '*.md text eol=lf\n'
        '*.tdn text eol=lf diff=tdn\n'
        '*.json text eol=lf\n'
        '*.tsv text eol=lf\n'
        '*.xml text eol=lf\n'
        '*.toe binary\n'
        '*.tox binary\n'
    )
    MARKER = 'Embody / Envoy'

    try:
        gitattr = git_root / '.gitattributes'
        existing = ''
        if gitattr.exists():
            existing = gitattr.read_text(encoding='utf-8')

        if MARKER in existing:
            # Migrate a managed block that predates the .tdn diff driver.
            if ('*.tdn text eol=lf diff=tdn' not in existing
                    and '*.tdn text eol=lf' in existing):
                existing = existing.replace(
                    '*.tdn text eol=lf', '*.tdn text eol=lf diff=tdn')
                gitattr.write_text(existing, encoding='utf-8')
                ext._log(
                    'Migrated .gitattributes: enabled .tdn semantic diff')
            else:
                ext._log('.gitattributes already configured', 'DEBUG')
            return

        if existing and not existing.endswith('\n'):
            existing += '\n'

        def _write():
            gitattr.write_text(existing + MANAGED_BLOCK, encoding='utf-8')
            ext._log('Added line-ending normalization to .gitattributes')
            try:  # record the marked block so Uninstall strips only it (never the user's file)
                Embody = op.Embody.ext.Embody
                Embody._manifestRecordAppendedFile(
                    str(Embody._findProjectRoot()), gitattr, MARKER)
            except Exception:
                pass

        # Advanced mode: confirm before editing the user's .gitattributes.
        op.Embody.ext.Embody._guardFileWrite(
            'Git config',
            f'add line-ending + .tdn-diff rules to .gitattributes in {git_root}',
            [ln for ln in MANAGED_BLOCK.strip().splitlines()
             if ln and not ln.startswith('#')],
            _write)

    except Exception as e:
        ext._log(f'Could not auto-configure .gitattributes: {e}', 'WARNING')


def configure_tdn_diff_driver(ext, target_dir, python_cmd):
    """Deploy the .tdn git textconv script and register it as a git diff
    driver in the repo. With the `*.tdn diff=tdn` attribute (set by
    _configureGitattributes), this makes `git diff` / `git log -p` /
    `git show` on .tdn files show only semantic network changes -- the
    volatile export header is stripped before diffing, so re-exporting an
    unchanged network produces an empty diff. This is the committed/on-disk
    counterpart to the live `diff_tdn` MCP tool. The driver definition must
    live in the repo's git config (git refuses to run textconv commands
    defined by a cloned repo), so Embody configures it the same way it
    manages .gitignore/.gitattributes/.mcp.json. Idempotent."""
    from pathlib import Path
    try:
        target_dir = Path(target_dir)
        embody_dir = target_dir / '.embody'
        embody_dir.mkdir(parents=True, exist_ok=True)
        script_path = embody_dir / 'tdn_textconv.py'

        # Source from the templates textDAT, else the dev/embody fallback.
        content = None
        try:
            templates = ext.ownerComp.op('templates')
            dat = templates.op('text_tdn_textconv') if templates else None
            if dat:
                content = dat.text
        except Exception:
            pass
        if not content:
            source = Path(project.folder) / 'embody' / 'tdn_textconv.py'
            if source.exists():
                content = source.read_text(encoding='utf-8')
        if not content:
            ext._log(
                'tdn_textconv source not found -- skipping .tdn diff driver',
                'DEBUG')
            return

        # Write only if changed, to avoid touching mtime needlessly.
        if not (script_path.exists()
                and script_path.read_text(encoding='utf-8') == content):
            script_path.write_text(content, encoding='utf-8')

        # Register the driver in the repo's git config (idempotent).
        script_str = str(script_path).replace('\\', '/')
        driver = '"%s" "%s"' % (python_cmd, script_str)
        git_kwargs = dict(cwd=str(target_dir), capture_output=True,
                          text=True, timeout=10,
                          stdin=subprocess.DEVNULL)
        current = subprocess.run(
            ['git', 'config', '--get', 'diff.tdn.textconv'], **git_kwargs)
        if (current.stdout or '').strip() != driver:
            def _write():
                subprocess.run(
                    ['git', 'config', 'diff.tdn.textconv', driver],
                    check=True, **git_kwargs)
                subprocess.run(
                    ['git', 'config', 'diff.tdn.cachetextconv', 'false'],
                    check=True, **git_kwargs)
                ext._log('Configured git diff driver for .tdn (semantic diffs)')
                try:  # record so Uninstall un-sets the repo git config
                    op.Embody.ext.Embody._manifestRecordGitConfig(
                        str(target_dir),
                        ['diff.tdn.textconv', 'diff.tdn.cachetextconv'])
                except Exception:
                    pass

            # Advanced: confirm before mutating the repo's .git/config.
            op.Embody.ext.Embody._guardFileWrite(
                'Git config',
                f'register the .tdn semantic-diff driver in '
                f'{target_dir}/.git/config',
                ['git config diff.tdn.textconv',
                 'git config diff.tdn.cachetextconv'],
                _write)

    except (subprocess.SubprocessError, OSError) as e:
        ext._log(f'Could not configure .tdn git diff driver: {e}', 'DEBUG')
    except Exception as e:
        ext._log(f'Could not deploy tdn_textconv: {e}', 'WARNING')


def cleanup_temp_files(ext):
    """Remove stale Envoy temp files (captures, offloaded responses) from /tmp.
    Deletes files older than 24 hours matching envoy_* patterns."""
    import glob
    import os

    tmp = tempfile.gettempdir()
    patterns = [os.path.join(tmp, 'envoy_capture_*'),
                os.path.join(tmp, 'envoy_query_network_*'),
                os.path.join(tmp, 'envoy_get_op_*')]
    cutoff = time.time() - 86400  # 24 hours ago
    removed = 0
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    if removed:
        ext._log(f'Cleaned up {removed} stale Envoy temp file(s)', 'DEBUG')
