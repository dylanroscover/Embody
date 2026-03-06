"""
Embody - Automatic TOX and DAT Externalization for TouchDesigner

Embody automatically creates, maintains and updates tox and DAT file
externalizations for your project, supporting a variety of file formats.

Simply add your preferred tags for COMPs/DATs to be saved, and on ctrl-s
external file references will automatically be created and/or updated.

Author: Dylan Roscover
"""

from __future__ import annotations

import os
import subprocess
import sys
import shutil
import inspect
from collections import deque
from datetime import datetime
from pathlib import Path
from glob import glob
from typing import Optional, Union, Any


class EmbodyExt:
    """
    Main extension class for Embody - manages externalization of
    TouchDesigner COMPs and DATs to external files.
    """

    # Template DAT name -> target path (relative to project root)
    # Used by _extractClaudeConfig() to generate .claude/ structure for user projects
    _TEMPLATE_MAP = {
        'text_rule_network_layout': '.claude/rules/network-layout.md',
        'text_rule_td_python': '.claude/rules/td-python.md',
        'text_rule_mcp_safety': '.claude/rules/mcp-safety.md',
        'text_skill_create_operator': '.claude/skills/create-operator/SKILL.md',
        'text_skill_debug_operator': '.claude/skills/debug-operator/SKILL.md',
        'text_skill_externalize': '.claude/skills/externalize-operator/SKILL.md',
        'text_skill_create_extension': '.claude/skills/create-extension/SKILL.md',
        'text_skill_manage_annotations': '.claude/skills/manage-annotations/SKILL.md',
        'text_skill_td_api_reference': '.claude/skills/td-api-reference/SKILL.md',
        'text_skill_mcp_tools_reference': '.claude/skills/mcp-tools-reference/SKILL.md',
    }

    # ==========================================================================
    # INITIALIZATION
    # ==========================================================================

    def __init__(self, ownerComp: COMP) -> None:
        self.my = ownerComp

        # Suppress TD ThreadManager's benign "fallback strategy" warning that
        # fires on every standalone EnqueueTask call (used by Envoy and TDN).
        import logging
        logging.getLogger('TDAppLogger.threadManager_logger').setLevel(logging.ERROR)

        self.lister = self.my.op('list/list1')
        self.tagging_menu_window = self.my.op('window_tagging_menu')
        self.tagger = self.my.op('tagger')
        self.root = op('/')
        self._tagger_mode = 'tag'  # 'tag' or 'manage'
        
        # Logging configuration
        self.header = 'Embody >'
        self.debug_mode = False  # Set to True for verbose path logging
        self._log_buffer = deque(maxlen=200)
        self._log_counter = 0
        self._fifo = self.my.op('fifo1')

        # Enable file logging by default
        if not self.my.par.Logfolder.eval():
            self.my.par.Logfolder = 'logs'
        if not self.my.par.Logtofile:
            self.my.par.Logtofile = True
        
        # Supported operator types for DAT externalization
        self.supported_dat_types = [
            'text', 'table', 'execute', 'parexec', 'pargroupexec',
            'chopexec', 'datexec', 'opexec', 'panelexec'
        ]

        # Mapping: DAT type -> default tag parameter name
        self.dat_type_to_tag = {
            'text': 'Pytag',
            'table': 'Tsvtag',
            'execute': 'Pytag',
            'parexec': 'Pytag',
            'pargroupexec': 'Pytag',
            'chopexec': 'Pytag',
            'datexec': 'Pytag',
            'opexec': 'Pytag',
            'panelexec': 'Pytag'
        }

        # Mapping: file extension/language -> tag parameter name
        self.extension_to_tag = {
            'csv': 'Csvtag', 'dat': 'Dattag', 'frag': 'Glsltag',
            'glsl': 'Glsltag', 'html': 'Htmltag', 'json': 'Jsontag',
            'md': 'Mdtag', 'py': 'Pytag', 'rtf': 'Rtftag',
            'tsv': 'Tsvtag', 'txt': 'Txttag', 'vert': 'Glsltag',
            'xml': 'Xmltag', 'yml': 'Jsontag', 'yaml': 'Jsontag',
            'python': 'Pytag', 'tscript': 'Pytag'
        }

        # Mapping: tag value -> language parameter value (for text DATs)
        self.tag_to_language = {
            'py': 'python', 'json': 'json', 'xml': 'xml',
            'html': 'xml', 'glsl': 'glsl', 'frag': 'glsl',
            'vert': 'glsl', 'txt': 'text',
        }

        # Tags where the extension parameter must be set explicitly
        # (language alone gives the wrong file extension, or no language mapping exists)
        self.tag_to_extension = {
            'html': 'html', 'frag': 'frag', 'vert': 'vert',
            'md': 'md', 'csv': 'csv', 'tsv': 'tsv',
            'rtf': 'rtf', 'dat': 'dat',
        }

        # Parameter tracker for detecting COMP changes
        self.param_tracker = ParameterTracker(self.my)

        # Network fingerprints for TDN COMPs — used instead of oper.dirty
        # (which is always True when externaltox is empty)
        self._tdn_fingerprints = {}

        # Set up Python environment (uv + venv) for Envoy dependencies
        # Only install if Envoy is enabled (user opted in during Verify)
        if self.my.par.Envoyenable.eval():
            self._setupEnvironment()

    # ==========================================================================
    # PYTHON ENVIRONMENT SETUP (uv)
    # ==========================================================================

    def _setupEnvironment(self):
        """
        Set up a Python virtual environment using uv for Envoy dependencies.
        Installs uv if not found, creates .venv, installs packages.
        Adds the venv's site-packages to sys.path so TD can import from it.
        """
        project_dir = project.folder
        venv_dir = os.path.join(project_dir, '.venv')

        # Platform-aware paths
        # Use sys.executable to get the current Python interpreter (cross-platform)
        python_exe = sys.executable
        if sys.platform.startswith('win'):
            site_packages = os.path.join(venv_dir, 'Lib', 'site-packages')
            venv_python = os.path.join(venv_dir, 'Scripts', 'python.exe')
        else:
            py_ver = f'python{sys.version_info.major}.{sys.version_info.minor}'
            site_packages = os.path.join(venv_dir, 'lib', py_ver, 'site-packages')
            venv_python = os.path.join(venv_dir, 'bin', 'python')

        # Dependencies - pywin32 is Windows-only
        # Bump MCP_MIN_VERSION when a new release is tested and verified
        MCP_MIN_VERSION = '1.26.0'
        deps = [f'mcp>={MCP_MIN_VERSION}']
        if sys.platform.startswith('win'):
            deps.append('pywin32>=306')

        # Fast path: if deps already installed and version sufficient, just add to sys.path
        if os.path.isdir(os.path.join(site_packages, 'mcp')):
            self._addSitePackages(site_packages)
            if sys.platform.startswith('win'):
                self._fixPywin32Dlls(site_packages)
            # Check installed version meets minimum
            try:
                from importlib.metadata import version as pkg_version
                installed = pkg_version('mcp')
                if tuple(int(x) for x in installed.split('.')) >= tuple(int(x) for x in MCP_MIN_VERSION.split('.')):
                    self._checkMCPUpdate(installed)
                    return
                self.Log(f'MCP {installed} installed, upgrading to >={MCP_MIN_VERSION}...')
            except Exception:
                return  # Can't determine version, assume OK

        try:
            uv = self._findOrInstallUv(python_exe)
            if not uv:
                return

            # Create venv if it doesn't exist
            if not os.path.isdir(venv_dir):
                self.Log('Creating virtual environment...')
                subprocess.run(
                    [uv, 'venv', venv_dir, '--python', python_exe],
                    check=True, capture_output=True, text=True
                )

            # Install dependencies
            self.Log('Installing dependencies...')
            subprocess.run(
                [uv, 'pip', 'install'] + deps + ['--python', venv_python],
                check=True, capture_output=True, text=True
            )

            self._addSitePackages(site_packages)
            if sys.platform.startswith('win'):
                self._fixPywin32Dlls(site_packages)
            self.Log('Python environment ready', 'SUCCESS')

        except subprocess.CalledProcessError as e:
            self.Log(f'Environment setup failed: {e.stderr or e}', 'ERROR')
        except Exception as e:
            self.Log(f'Environment setup failed: {e}', 'ERROR')

    def _findOrInstallUv(self, python_exe):
        """Find uv on PATH, or install it via pip --user. Returns path to uv executable or None."""
        # Check PATH first
        uv = shutil.which('uv')
        if uv:
            return uv

        # Install uv via pip --user (avoids needing admin for Program Files)
        self.Log('uv not found - installing via pip...')
        try:
            subprocess.run(
                [python_exe, '-m', 'pip', 'install', '--user', 'uv'],
                check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as e:
            self.Log(f'Failed to install uv: {e.stderr or e}', 'ERROR')
            return None

        # Find the installed uv binary in user Scripts directories
        uv = shutil.which('uv')
        if uv:
            return uv

        # Search common --user install locations (platform-specific)
        if sys.platform.startswith('win'):
            appdata = os.environ.get('APPDATA', '')
            if appdata:
                candidates = glob(os.path.join(appdata, 'Python', 'Python*', 'Scripts', 'uv.exe'))
                for candidate in candidates:
                    if os.path.isfile(candidate):
                        return candidate
        else:
            # macOS: check common user-local bin directories
            home = os.path.expanduser('~')
            mac_candidates = (
                glob(os.path.join(home, 'Library', 'Python', '3.*', 'bin', 'uv'))
                + [os.path.join(home, '.local', 'bin', 'uv')]
            )
            for candidate in mac_candidates:
                if os.path.isfile(candidate):
                    return candidate

        self.Log('Could not find uv after install - is Python user Scripts on PATH?', 'ERROR')
        return None

    def _addSitePackages(self, site_packages):
        """Add venv site-packages (and pywin32 subdirs on Windows) to sys.path."""
        paths = [site_packages]
        if sys.platform.startswith('win'):
            paths.append(os.path.join(site_packages, 'win32'))
            paths.append(os.path.join(site_packages, 'win32', 'lib'))
        for p in paths:
            if os.path.isdir(p) and p not in sys.path:
                sys.path.insert(0, p)

    def _fixPywin32Dlls(self, site_packages):
        """Copy pywin32 DLLs to win32/ so they're importable without post-install."""
        src_dir = os.path.join(site_packages, 'pywin32_system32')
        dst_dir = os.path.join(site_packages, 'win32')
        if not os.path.isdir(src_dir) or not os.path.isdir(dst_dir):
            return
        for dll in os.listdir(src_dir):
            if dll.endswith('.dll'):
                src = os.path.join(src_dir, dll)
                dst = os.path.join(dst_dir, dll)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)

    def _checkMCPUpdate(self, installed: str):
        """Check PyPI for a newer MCP version in a background thread. Logs a
        notice if an update is available - never blocks the main thread."""
        import threading

        def _check():
            try:
                import urllib.request
                import json
                req = urllib.request.Request(
                    'https://pypi.org/pypi/mcp/json',
                    headers={'Accept': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                latest = data['info']['version']
                if tuple(int(x) for x in latest.split('.')) > tuple(int(x) for x in installed.split('.')):
                    self.Log(
                        f'MCP update available: {installed} -> {latest}. '
                        f'Update MCP_MIN_VERSION in EmbodyExt._setupEnvironment() '
                        f'and delete dev/.venv to upgrade.',
                        'WARNING'
                    )
            except Exception:
                pass  # Network unavailable, not critical

        threading.Thread(target=_check, daemon=True).start()

    # ==========================================================================
    # PROPERTIES
    # ==========================================================================

    @property
    def Externalizations(self) -> Optional[DAT]:
        """Returns the externalizations table DAT."""
        return self.my.par.Externalizations.eval()

    @property
    def ExternalizationsFolder(self) -> str:
        """Returns the configured externalization folder, or empty string."""
        return self.my.par.Folder.eval() or ''

    # ==========================================================================
    # PATH UTILITIES - Cross-Platform Support
    # ==========================================================================

    def normalizePath(self, path_str: Union[str, Path, None]) -> str:
        """
        Normalize path separators to forward slashes for cross-platform compatibility.
        Forward slashes work on both Windows and macOS.
        """
        return str(path_str).replace('\\', '/') if path_str else path_str

    def _safeSyncFile(self, op_path, value):
        """Set syncfile on an operator if it still exists."""
        o = op(op_path)
        if o:
            o.par.syncfile = value

    def _safeAllowCooking(self, op_path, value):
        """Set allowCooking on an operator if it still exists."""
        o = op(op_path)
        if o:
            o.allowCooking = value

    def getExternalPath(self, oper: OP) -> str:
        """Get the normalized external file path from an operator."""
        if oper.family == 'COMP':
            return self.normalizePath(oper.par.externaltox.eval())
        elif oper.family == 'DAT':
            return self.normalizePath(oper.par.file.eval())
        return ''

    def setExternalPath(self, oper: OP, path_str: str, readonly: bool = True) -> None:
        """Set the external file path on an operator (normalized)."""
        normalized = self.normalizePath(path_str)
        if oper.family == 'COMP':
            oper.par.externaltox.readOnly = False
            oper.par.externaltox = normalized
            oper.par.externaltox.readOnly = readonly
        elif oper.family == 'DAT':
            oper.par.file.readOnly = False
            oper.par.file = normalized
            oper.par.file.readOnly = readonly

    def buildAbsolutePath(self, rel_path: Union[str, Path]) -> Path:
        """Build absolute path from relative path, handling cross-platform issues."""
        return Path(project.folder) / self.normalizePath(rel_path)

    def getOpPaths(self, opToExternalize: OP, externalizationsFolder: Optional[str] = None) -> tuple[Optional[Path], Optional[Path], Optional[str], Optional[str]]:
        """
        Generate file paths for an operator's externalization.

        Returns:
            tuple: (abs_folder_path, save_file_path, rel_directory, rel_file_path)
                   or (None, None, None, None) on error
        """
        if externalizationsFolder is None or externalizationsFolder is False:
            externalizationsFolder = self.ExternalizationsFolder
        
        # Normalize folder path
        if externalizationsFolder:
            externalizationsFolder = self.normalizePath(externalizationsFolder)

        # If operator already has an external path, use it
        existing_path = self.getExternalPath(opToExternalize)
        if existing_path:
            rel_file_path = existing_path
            abs_folder_path = self.buildAbsolutePath(rel_file_path).parent
            save_file_path = self.buildAbsolutePath(rel_file_path)
            rel_directory = self.normalizePath(str(Path(rel_file_path).parent))
            return abs_folder_path, save_file_path, rel_directory, rel_file_path

        # Determine file extension
        if opToExternalize.family == 'COMP':
            file_extension = '.tox'
        elif opToExternalize.family == 'DAT':
            tags = self.getTags()
            found = [tag for tag in opToExternalize.tags if tag in tags]
            file_extension = f'.{found[0]}' if found else None
        else:
            file_extension = None

        if file_extension is None:
            self.Log("File extension not found", "ERROR")
            return None, None, None, None

        # Build paths
        filename = opToExternalize.name + file_extension
        parent_path = str(opToExternalize.parent().path).strip('/')
        parent_components = [p for p in parent_path.split('/') if p]
        
        # Combine folder and parent components
        path_parts = []
        if externalizationsFolder:
            path_parts.append(externalizationsFolder)
        path_parts.extend(parent_components)
        
        if path_parts:
            rel_directory = '/'.join(path_parts)
            rel_file_path = f'{rel_directory}/{filename}'
        else:
            # Root-level operator with no externalizations folder
            rel_directory = ''
            rel_file_path = filename
        
        abs_folder_path = Path(project.folder) / rel_directory if rel_directory else Path(project.folder)
        save_file_path = Path(project.folder) / rel_file_path
        
        if self.debug_mode:
            self.Log(f"getOpPaths for {opToExternalize.path}:", "INFO")
            self.Log(f"  rel_directory: {rel_directory}", "INFO")
            self.Log(f"  rel_file_path: {rel_file_path}", "INFO")
            self.Log(f"  abs_folder_path: {abs_folder_path}", "INFO")
            self.Log(f"  save_file_path: {save_file_path}", "INFO")
        
        return abs_folder_path, save_file_path, rel_directory, rel_file_path

    # ==========================================================================
    # ENVOY ONBOARDING
    # ==========================================================================

    def _promptEnvoy(self):
        """Prompt user to enable Envoy (AI coding assistant integration)."""
        choice = ui.messageBox('Embody - AI Coding Assistant Integration',
            'Enable Envoy?\n\n'
            'Envoy is an MCP server that lets AI coding assistants\n'
            'create, modify, and query TouchDesigner operators.\n\n'
            'This will:\n'
            '  - Install Python dependencies (~30 MB)\n'
            '  - Start a local MCP server on port '
            f'{self.my.par.Envoyport.eval()}\n'
            '  - Create CLAUDE.md and .mcp.json in your project\n\n'
            'Works with Claude Code, Cursor, Windsurf, and other MCP clients.\n'
            'You can change this later via the Envoyenable parameter.',
            buttons=['Skip', 'Enable Envoy'])

        if choice == 1:
            self._enableEnvoy()
        else:
            self.my.par.Envoyenable = False
            self.Log('Envoy skipped. Enable later via Envoyenable parameter.', 'INFO')

    def _enableEnvoy(self):
        """Enable Envoy: install deps, extract Claude config, start server."""
        self.Log('Setting up Envoy...', 'INFO')

        # Install Python dependencies
        self._setupEnvironment()

        # Extract CLAUDE.md and .claude/ structure to project/repo root
        self._extractClaudeConfig()

        # Enable Envoy (triggers Start() via parexec.py)
        self.my.par.Envoyenable = True

        self.Log(
            'Envoy enabled! Start your AI coding assistant and connect via MCP.',
            'SUCCESS'
        )

    def _findProjectRoot(self):
        """Find the git root, or fall back to project.folder."""
        project_dir = Path(project.folder)
        for parent_dir in [project_dir] + list(project_dir.parents):
            if (parent_dir / '.git').exists():
                return parent_dir
        self.Log(
            'No git repo found. Writing config to project folder. '
            'For best results, place your .toe inside a git repo.',
            'INFO'
        )
        return project_dir

    def _extractClaudeConfig(self):
        """Extract CLAUDE.md and .claude/ structure to the project/repo root."""
        target_dir = self._findProjectRoot()

        # 1. Write CLAUDE.md (with ENVOY.md fallback)
        self._writeClaudeMd(target_dir)

        # 2. Write .claude/rules/ and .claude/skills/ from template DATs
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log(
                'Templates COMP not found — skipping .claude/ generation',
                'DEBUG'
            )
            return

        written = 0
        for dat_name, rel_path in self._TEMPLATE_MAP.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat:
                continue
            content = template_dat.text
            if not content:
                continue
            if self._writeTemplate(target_dir, rel_path, content):
                written += 1

        if written > 0:
            self.Log(
                f'Generated {written} .claude/ files at {target_dir}',
                'SUCCESS'
            )

    def _writeClaudeMd(self, target_dir):
        """Write CLAUDE.md from the text_claude template DAT."""
        template_dat = self.my.op('text_claude')
        if not template_dat:
            self.Log('CLAUDE.md template DAT not found inside Embody', 'WARNING')
            return None

        content = template_dat.text
        if not content:
            self.Log('CLAUDE.md template DAT is empty', 'WARNING')
            return None

        claude_md_path = target_dir / 'CLAUDE.md'

        if claude_md_path.exists():
            existing = claude_md_path.read_text(encoding='utf-8')
            if '<!-- Generated by Embody/Envoy' in existing:
                claude_md_path.write_text(content, encoding='utf-8')
                self.Log(f'Updated CLAUDE.md at {claude_md_path}', 'SUCCESS')
            else:
                fallback = target_dir / 'ENVOY.md'
                fallback.write_text(content, encoding='utf-8')
                self.Log(
                    f'CLAUDE.md already exists (not generated by Embody). '
                    f'Wrote MCP reference to {fallback} instead.',
                    'WARNING'
                )
                return fallback
        else:
            claude_md_path.write_text(content, encoding='utf-8')
            self.Log(f'Created CLAUDE.md at {claude_md_path}', 'SUCCESS')

        return claude_md_path

    def _writeTemplate(self, target_dir, rel_path, content):
        """Write a single template file, respecting the Embody/Envoy marker.

        Returns True if the file was written, False if skipped.
        """
        target_path = target_dir / rel_path
        if target_path.exists():
            existing = target_path.read_text(encoding='utf-8')
            if '<!-- Generated by Embody/Envoy' not in existing:
                return False
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding='utf-8')
        return True

    def _upgradeEnvoy(self):
        """Silently extract Claude config if Envoy is enabled but files are missing."""
        if not self.my.par.Envoyenable.eval():
            return
        target_dir = self._findProjectRoot()
        needs_extract = (
            not (target_dir / 'CLAUDE.md').exists()
            and not (target_dir / 'ENVOY.md').exists()
        ) or not (target_dir / '.claude' / 'rules').exists()
        if needs_extract:
            self._extractClaudeConfig()

    # ==========================================================================
    # INITIALIZATION & RESET
    # ==========================================================================

    def Reset(self, removeTags: bool = False) -> None:
        """Reset Embody to initial state."""
        parent.Embody.Disable(False, removeTags)
        run(f"op('{self.my}').UpdateHandler()", delayFrames=10)
        self.createExternalizationsTable()
        self.my.par.externaltox = ''

    def createExternalizationsTable(self) -> None:
        """Create or reset the externalizations tracking table."""
        table_name = 'externalizations'
        externalizations_dat = self.Externalizations
        
        if not externalizations_dat:
            # Create new table
            externalizations_dat = self.my.parent().create(tableDAT, table_name)
            externalizations_dat.nodeX = self.my.nodeX - 200
            externalizations_dat.nodeY = self.my.nodeY
            externalizations_dat.dock = self.my
            externalizations_dat.color = (
                self.my.par.Dattagcolorr,
                self.my.par.Dattagcolorg,
                self.my.par.Dattagcolorb
            )
            externalizations_dat.clear()
            externalizations_dat.appendRow([
                'path', 'type', 'strategy', 'rel_file_path', 'timestamp',
                'dirty', 'build', 'touch_build'
            ])
            externalizations_dat.tags = [self.my.par.Tsvtag.eval()]
            self.Log(f"Created '{table_name}' tableDAT", "SUCCESS")
        else:
            externalizations_dat.clear(keepFirstRow=True)
            self.Log(f"Reset '{table_name}' tableDAT", "INFO")
        
        self.my.par.Externalizations.val = externalizations_dat

    def _migrateTableSchema(self) -> None:
        """Migrate externalizations table schema to current version.

        Adds missing columns (strategy, node_x, node_y, node_color),
        populates them from existing data, and removes legacy rows.
        """
        table = self.Externalizations
        if not table or table.numRows < 1:
            return

        headers = [table[0, c].val for c in range(table.numCols)]

        migrations = []

        # Migration 1: Add strategy column (v5.0.176+)
        if 'strategy' not in headers:
            type_idx = headers.index('type') if 'type' in headers else 1
            strategy_col = type_idx + 1
            table.insertCol('', strategy_col)
            table[0, strategy_col] = 'strategy'

            # Collect TDN companion rows to remove (iterate backwards)
            rows_to_delete = []
            for i in range(1, table.numRows):
                row_type = table[i, 'type'].val
                rel_path = table[i, 'rel_file_path'].val

                if row_type == 'tdn':
                    rows_to_delete.append(i)
                    continue

                oper = op(table[i, 'path'].val)
                if oper and oper.family == 'COMP':
                    table[i, 'strategy'] = 'tox'
                elif rel_path:
                    ext = rel_path.rsplit('.', 1)[-1] if '.' in rel_path else ''
                    table[i, 'strategy'] = ext
                else:
                    table[i, 'strategy'] = row_type

            for i in reversed(rows_to_delete):
                table.deleteRow(i)

            count = len(rows_to_delete)
            if count:
                migrations.append(f'strategy column (removed {count} legacy TDN row(s))')
            else:
                migrations.append('strategy column')

            # Refresh headers after modification
            headers = [table[0, c].val for c in range(table.numCols)]

        # Migration 2: Add position/color columns (v5.0.189+)
        if 'node_x' not in headers:
            table.appendCol('node_x')
            table.appendCol('node_y')
            table.appendCol('node_color')
            table[0, table.numCols - 3] = 'node_x'
            table[0, table.numCols - 2] = 'node_y'
            table[0, table.numCols - 1] = 'node_color'
            migrations.append('node_x/node_y/node_color columns')

        if migrations:
            self.Log(f'Schema migration: added {", ".join(migrations)}', 'SUCCESS')

    def Verify(self) -> None:
        """Verify Embody instance and prompt for initialization."""
        embodies = op('/').findChildren(name='Embody', parName='Addtagshort')
        other_embody = next((e for e in embodies if e != self.my), None)

        if other_embody:
            ui.messageBox('Embody',
                f'An instance of Embody already exists:\n{other_embody}\n'
                'Please remove it first.', buttons=['Ok'])
            return

        # Stage 1: Standard Embody initialization
        if not ui.messageBox('Embody',
            'Initialize Embody from previously saved state?\n'
            '(If unsure, select "Yes")',
            buttons=['No', 'Yes']):
            return  # User declined -- stop here

        self.Reset()

        # Stage 2: Envoy opt-in (deferred so Reset() completes first)
        run(f"op('{self.my}').ext.Embody._promptEnvoy()", delayFrames=5)

    # ==========================================================================
    # SAFE FILE TRACKING
    # ==========================================================================

    def getTrackedFilePaths(self) -> set[Path]:
        """
        Get a set of all file paths that Embody has created/is tracking.
        These are the ONLY files Embody should ever delete.

        Returns:
            set: Absolute Path objects of all tracked files
        """
        tracked = set()
        
        if not self.Externalizations:
            return tracked
            
        for i in range(1, self.Externalizations.numRows):
            rel_file_path = self.Externalizations[i, 'rel_file_path'].val
            if rel_file_path:
                abs_path = self.buildAbsolutePath(self.normalizePath(rel_file_path)).resolve()
                tracked.add(abs_path)
        
        return tracked

    def isTrackedFile(self, file_path: Union[str, Path]) -> bool:
        """
        Check if a file path is tracked by Embody.

        Args:
            file_path: Path object or string to check

        Returns:
            bool: True if this file is in our externalizations table
        """
        if isinstance(file_path, str):
            file_path = Path(file_path)
        
        resolved = file_path.resolve()
        return resolved in self.getTrackedFilePaths()

    def safeDeleteFile(self, file_path: Union[str, Path], force: bool = False) -> bool:
        """
        Safely delete a file, but ONLY if it's tracked by Embody.

        Args:
            file_path: Path object or string of the file to delete
            force: If True, delete even if not tracked (use with extreme caution!)

        Returns:
            bool: True if file was deleted, False otherwise
        """
        if isinstance(file_path, str):
            file_path = Path(file_path)
        
        resolved = file_path.resolve()
        
        if not resolved.is_file():
            return False
        
        if not force and not self.isTrackedFile(resolved):
            self.Log(f"SAFETY: Refusing to delete untracked file: {resolved}", "WARNING")
            return False
        
        try:
            resolved.unlink()
            self.Log(f"Deleted tracked file: {resolved}", "INFO")
            return True
        except Exception as e:
            self.Log(f"Error deleting file: {resolved}", "ERROR", str(e))
            return False

    def safeDeleteTrackedFiles(self, folder_path: Union[str, Path]) -> tuple[int, int]:
        """
        Delete only the files in a folder that Embody is tracking.
        Non-Embody files are left untouched.

        Args:
            folder_path: Path to scan for tracked files

        Returns:
            tuple: (deleted_count, skipped_count)
        """
        if isinstance(folder_path, str):
            folder_path = Path(folder_path)
        
        if not folder_path.exists():
            return (0, 0)
        
        tracked_files = self.getTrackedFilePaths()
        deleted = 0
        skipped = 0
        
        # Walk through folder and delete only tracked files
        for file_path in folder_path.rglob('*'):
            if file_path.is_file():
                resolved = file_path.resolve()
                if resolved in tracked_files:
                    try:
                        resolved.unlink()
                        self.Log(f"Deleted tracked file: {resolved}", "INFO")
                        deleted += 1
                    except Exception as e:
                        self.Log(f"Error deleting: {resolved}", "ERROR", str(e))
                else:
                    skipped += 1
        
        if skipped > 0:
            self.Log(f"SAFETY: Preserved {skipped} untracked file(s) in {folder_path}", "INFO")
        
        return (deleted, skipped)

    # ==========================================================================
    # ENABLE / DISABLE
    # ==========================================================================

    def Disable(self, prevFolder: Union[str, bool, None] = False, removeTags: Union[bool, int] = False) -> None:
        """
        Disable Embody: clear external paths and optionally delete files/tags.
        SAFETY: Only deletes files that Embody is tracking - never deletes
        untracked files that may exist in the externalization folder.
        """
        folder = self.ExternalizationsFolder if prevFolder is None else prevFolder
        if prevFolder == '':
            folder = project.folder

        tags = self.getTags()
        
        # Collect all tracked file paths BEFORE clearing operator references
        tracked_files = self.getTrackedFilePaths()
        self.Log(f"Disable: Found {len(tracked_files)} tracked file(s) to clean up", "INFO")
        
        # Clear COMP externalizations
        for oper in self.getExternalizedOps(COMP):
            oper.par.externaltox = ''
            if removeTags:
                for tag in tags:
                    if tag in oper.tags:
                        oper.tags.remove(tag)
                self.resetOpColor(oper)

        # Clear DAT externalizations
        for oper in self.getExternalizedOps(DAT):
            try:
                oper.par.syncfile = False
                oper.par.file = ''
            except Exception as e:
                self.Log(f"Failed to clear file params on {oper.path}: {e}", "DEBUG")
                pass
            if removeTags and str(self.Externalizations) not in oper.path:
                for tag in tags:
                    if tag in oper.tags:
                        oper.tags.remove(tag)
                self.resetOpColor(oper)

        # Remove tags from ALL project operators (catches untracked tagged ops)
        if removeTags:
            tag_set = set(tags)
            for oper in self.root.findChildren():
                found = set(oper.tags) & tag_set
                if found:
                    for tag in found:
                        oper.tags.remove(tag)
                    self.resetOpColor(oper)

        # SAFELY delete only tracked files
        deleted_count = 0
        for tracked_file in tracked_files:
            if tracked_file.is_file():
                try:
                    tracked_file.unlink()
                    deleted_count += 1
                except Exception as e:
                    self.Log(f"Error deleting tracked file: {tracked_file}", "ERROR", str(e))
        
        if deleted_count > 0:
            self.Log(f"Deleted {deleted_count} tracked file(s)", "SUCCESS")

        # Clean up empty directories only (safe operation)
        self._cleanupEmptyDirectories(folder, prevFolder)

        # Clear externalizations table synchronously (no delay — delayed clear
        # creates a race condition if re-enabled before the callback fires)
        if self.Externalizations:
            self.Externalizations.clear(keepFirstRow=True)

        self.my.par.Status = 'Disabled'
        
        if folder:
            run(lambda: self.deleteEmptyDirectories(folder), delayFrames=60)
        
        self.Log("Disabled", "SUCCESS")

    def _cleanupEmptyDirectories(self, folder, prevFolder):
        """
        Helper to clean up empty directories after disable.
        SAFETY: Only removes directories that are completely empty.
        Never uses rmtree or deletes directories with contents.
        """
        if not folder:
            return
            
        # Remove empty top-level comp directories
        for comp in self.root.findChildren(depth=1, type=COMP):
            if comp.name not in ['local', 'perform']:
                comp_path = Path(f'{folder}/{comp.name}')
                if comp_path.is_dir():
                    try:
                        # rmdir() only succeeds if directory is empty - this is safe
                        comp_path.rmdir()
                    except OSError:
                        # Directory not empty - this is expected and safe to ignore
                        pass
                    except Exception as e:
                        self.Log(f"Error removing directory: {comp_path}", "ERROR", str(e))

        # Try to remove main folder only if empty
        try:
            if folder:
                folder_path = Path(folder)
                if folder_path.is_dir():
                    folder_path.rmdir()  # Only succeeds if empty
        except OSError:
            # Directory not empty - this is expected and safe
            pass
        except Exception as e:
            self.Log(f"Unexpected error removing directory {folder}: {e}", "WARNING")
            pass

        # Handle previous folder - SAFELY remove only if empty
        # NEVER use shutil.rmtree here!
        if prevFolder and prevFolder != self.getProjectFolder():
            prev_path = Path(prevFolder)
            if prev_path.is_dir() and prev_path != Path(self.getProjectFolder()):
                try:
                    # Only remove if empty - safe operation
                    prev_path.rmdir()
                    self.Log(f"Removed empty previous folder: {prev_path}", "INFO")
                except OSError:
                    # Not empty - preserve it!
                    self.Log(f"Previous folder not empty, preserving: {prev_path}", "INFO")
                except Exception as e:
                    self.Log(f"Error with previous folder: {prev_path}", "ERROR", str(e))

    def DisableHandler(self) -> None:
        """Handle disable button with confirmation dialog."""
        choice = ui.messageBox('Embody Warning',
            'Disable Embody?\nOnly files created by Embody will be deleted.\n'
            '(Non-Embody files in the folder will be preserved)',
            buttons=['No', 'Yes, keep Tags', 'Yes, remove Tags'])
        if choice == 1:
            self.Disable(self.ExternalizationsFolder, False)
        elif choice == 2:
            self.Disable(self.ExternalizationsFolder, True)

    def UpdateHandler(self) -> None:
        """Enable/Update handler - main entry point for initialization."""
        if self.my.par.Status == 'Disabled':
            self.Log("Enabled", "SUCCESS")
            self.my.par.Status = 'Enabled'
            self.param_tracker.initializeTracking(self)
            
            # Create externalization folder
            folder = self.getProjectFolder()
            try:
                os.mkdir(folder)
                self.Log(f"Created folder '{folder}'", "SUCCESS")
            except FileExistsError:
                pass

        # Migrate table schema if needed (adds strategy column)
        self._migrateTableSchema()

        # Normalize paths for cross-platform compatibility
        self.normalizeAllPaths()
        run(f"op('{self.my}').Update()", delayFrames=1)

    def normalizeAllPaths(self) -> None:
        """Normalize all paths in table and on operators for cross-platform support."""
        if not self.Externalizations:
            return
            
        paths_fixed = 0
        for i in range(1, self.Externalizations.numRows):
            rel_file_path = self.Externalizations[i, 'rel_file_path'].val
            normalized = self.normalizePath(rel_file_path)
            
            if rel_file_path != normalized:
                self.Externalizations[i, 'rel_file_path'] = normalized
                paths_fixed += 1
                
            # Update operator parameter if needed
            op_path = self.Externalizations[i, 'path'].val
            oper = op(op_path)
            if oper:
                current = self.getExternalPath(oper)
                if current and current != self.normalizePath(current):
                    self.setExternalPath(oper, self.normalizePath(current))
        
        if paths_fixed > 0:
            self.Log(f"Normalized {paths_fixed} path(s) for cross-platform compatibility", "SUCCESS")

    # ==========================================================================
    # MAIN UPDATE LOOP
    # ==========================================================================

    def Update(self, suppress_refresh: bool = False) -> None:
        """Main update method - process additions, subtractions, and dirty ops.

        Args:
            suppress_refresh: If True, skip the delayed Refresh pulse. Used by
                onProjectPreSave() to prevent the continuity check from firing
                during the TDN strip/restore window.
        """
        if self.my.par.Status != 'Enabled':
            return

        # Detect renames/moves BEFORE scanning for additions.
        # Without this, a renamed op gets added as "new" by the additions
        # scan, and the subsequent continuity check in Refresh() can't
        # match the stale entry because the new op is already tracked.
        self.checkOpsForContinuity(self.ExternalizationsFolder)

        # Check for parameter changes on TOX-strategy COMPs
        for comp in self.getExternalizedOps(COMP, strategy='tox'):
            if self.param_tracker.compareParameters(comp):
                self.Externalizations[comp.path, 'dirty'] = 'Par'
                self.Save(comp.path)

        # Check for parameter changes on TDN-strategy COMPs
        # Skip root "/" — it's a Full Project export, not a managed COMP.
        # SaveTDN("/") would trigger root-level stale cleanup that deletes
        # other tracked .tdn files.
        tdn_comps = self.getExternalizedOps(COMP, strategy='tdn')
        tdn_paths = {comp.path for comp in tdn_comps}
        for comp in tdn_comps:
            if comp.path == '/':
                continue
            if self.param_tracker.compareParameters(comp):
                self.Externalizations[comp.path, 'dirty'] = 'Par'
                self.SaveTDN(comp.path)

        # Check for duplicates
        if self.my.par.Detectduplicatepaths:
            self.checkForDuplicates()

        # Get operator lists
        all_tags = self.getTags()
        ops_to_externalize = self.getOpsToExternalize(COMP) + self.getOpsToExternalize(DAT)
        externalized_ops = self.getExternalizedOps(COMP) + self.getExternalizedOps(DAT)
        externalized_paths = [ext.path for ext in externalized_ops]

        # Find additions and subtractions
        additions = [
            oper for oper in ops_to_externalize
            if oper.path not in externalized_paths
            and set(all_tags).intersection(oper.tags)
            and self.isOpProcessable(oper)
        ]

        # TDN-strategy COMPs are excluded — their lifecycle is managed by
        # ToggleTag() → _removeTDNStrategy(), not by tag-presence detection.
        # Without this, Full Project TDN exports (which track "/" in the table
        # without tagging the root) get incorrectly removed as "subtractions".
        subtractions = [
            oper for oper in externalized_ops
            if oper.path not in tdn_paths
            and not set(all_tags).intersection(oper.tags)
            and not oper.warnings()
            and not oper.scriptErrors()
            and self.isOpProcessable(oper)
        ]

        # Process changes
        additions.sort(key=lambda x: (self.Externalizations.path in x.path, x.path), reverse=True)

        for oper in additions:
            self.handleAddition(oper)
        for oper in subtractions:
            self.handleSubtraction(oper)

        # Handle dirty COMPs (TOX + TDN)
        dirties = self.dirtyHandler(True)

        # Report results
        self._reportResults(dirties, additions, subtractions)
        if not suppress_refresh:
            run(f"op('{self.my}').par.Refresh.pulse()", delayFrames=1)

    def _reportResults(self, dirties, additions, subtractions):
        """Report update results to log."""
        plural = any(len(lst) > 1 for lst in [dirties, additions, subtractions])
        if dirties:
            self.Log(f"Saved {len(dirties)} externalization{'s' if plural else ''}", "SUCCESS")
        if additions:
            self.Log(f"Added {len(additions)} operator{'s' if plural else ''} in total", "SUCCESS")
        if subtractions:
            self.Log(f"Removed {len(subtractions)} operator{'s' if plural else ''} in total", "SUCCESS")

    def Refresh(self) -> None:
        """Refresh Embody state and UI."""
        self.cleanupAllDuplicateRows()
        self.updateDirtyStates(self.ExternalizationsFolder)
        self.my.op('list/inject_parents').cook(force=True)
        self.lister.reset()
        self.checkOpsForContinuity(self.ExternalizationsFolder)
        
        if self.my.par.Detectduplicatepaths:
            self.checkForDuplicates()
        
        self.Debug("Refreshed")
        
        if not me.time.play:
            self.Log("ALERT! TIMELINE IS PAUSED. RESUME FOR EMBODY TO FUNCTION", "ERROR")

    # ==========================================================================
    # OPERATOR QUERIES
    # ==========================================================================

    def getTags(self, selection: Optional[str] = None) -> list[str]:
        """Get all Embody tags, optionally filtered by type.

        Args:
            selection: 'tox' for TOX tag only, 'tdn' for TDN tag only,
                       'comp' for both COMP tags, 'DAT' for DAT tags only,
                       None for all tags.
        """
        tags = [par.val for par in self.my.pars('*tag')]
        if selection == 'tox':
            return [t for t in tags if t == self.my.par.Toxtag.val]
        elif selection == 'tdn':
            return [t for t in tags if t == self.my.par.Tdntag.val]
        elif selection == 'comp':
            comp_tags = {self.my.par.Toxtag.val, self.my.par.Tdntag.val}
            return [t for t in tags if t in comp_tags]
        elif selection == 'DAT':
            comp_tags = {self.my.par.Toxtag.val, self.my.par.Tdntag.val}
            return [t for t in tags if t not in comp_tags]
        return tags

    def getExternalizedOps(self, opFamily: type, strategy: Optional[str] = None) -> list[OP]:
        """Get all externalized operators of a given family from the table.

        Args:
            opFamily: COMP or DAT
            strategy: Optional filter — 'tox', 'tdn', or None for all.
        """
        if not self.Externalizations:
            return []

        family_str = 'COMP' if opFamily == COMP else 'DAT'
        has_strategy_col = 'strategy' in [
            self.Externalizations[0, c].val
            for c in range(self.Externalizations.numCols)
        ]
        ops = []

        for i in range(1, self.Externalizations.numRows):
            # Filter by strategy if requested
            if has_strategy_col and strategy:
                row_strategy = self.Externalizations[i, 'strategy'].val
                if row_strategy != strategy:
                    continue
            elif not has_strategy_col:
                # Legacy table without strategy column — skip TDN rows
                if self.Externalizations[i, 'type'].val == 'tdn':
                    continue

            path = self.Externalizations[i, 'path'].val
            oper = op(path)
            if oper and oper.family == family_str:
                if not oper.path.startswith('/local/') and oper.path != '/local':
                    ops.append(oper)

        return sorted(ops, key=lambda x: -x.path.count('/'))

    def getOpsToExternalize(self, opFamily: type) -> list[OP]:
        """Get all operators marked for externalization."""
        base_filter = lambda x: (
            self.isOpEligibleToBeExternalized(x) and
            not x.path.startswith('/local/') and
            x.path != '/local' and
            x.type != 'engine'
        )

        if opFamily == COMP:
            # TOX-tagged COMPs (have externaltox parameter)
            tox_tags = self.getTags('tox')
            tox_ops = self.root.findChildren(
                type=COMP, tags=tox_tags, parName='externaltox',
                key=base_filter
            )
            # TDN-tagged COMPs (no externaltox needed)
            tdn_tags = self.getTags('tdn')
            tdn_ops = self.root.findChildren(
                type=COMP, tags=tdn_tags,
                key=base_filter
            )
            return tox_ops + tdn_ops
        else:
            tags = self.getTags('DAT')
            return self.root.findChildren(
                type=DAT, tags=tags, parName='file',
                key=base_filter
            )

    def getOpsByPar(self, opFamily: type) -> list[OP]:
        """Get operators that have external paths set."""
        if opFamily == COMP:
            return self.root.findChildren(
                type=COMP,
                key=lambda x: (
                    x.par.externaltox.eval() != '' and
                    x.type not in ['engine', 'time', 'annotate']
                )
            )
        else:
            return self.root.findChildren(
                type=DAT,
                parName='file',
                key=lambda x: x.par.file.eval() != '',
                path='^/local/shortcuts'
            )

    def isOpEligibleToBeExternalized(self, oper: OP) -> bool:
        """Check if an operator can be externalized."""
        if oper.family == 'COMP':
            return True
        
        if oper.type not in self.supported_dat_types:
            return False
            
        dat_tags = self.getTags('DAT')
        has_tag = any(tag in oper.tags for tag in dat_tags)
        
        if not has_tag:
            return False
            
        return True

    def isOpProcessable(self, oper: OP) -> bool:
        """Check if operator should be processed (not clone/replicant/local)."""
        return (
            not self.isReplicant(oper) and
            not self.isInsideClone(oper) and
            not oper.path.startswith('/local/') and
            oper.path != '/local' and
            oper.type not in ['engine', 'time', 'annotate']
        )

    def isInsideClone(self, oper: OP) -> bool:
        """Check if operator is inside a clone hierarchy."""
        while oper:
            if oper.family == 'COMP' and oper.par.clone.eval():
                if oper.name not in str(oper.par.clone.eval()):
                    return True
            oper = oper.parent()
        return False

    def isClone(self, oper: OP) -> bool:
        """Check if operator is a clone (not master)."""
        if oper.family == 'COMP' and oper.par.clone.eval():
            return oper.name not in str(oper.par.clone.eval())
        return False

    def isReplicant(self, oper: OP) -> bool:
        """Check if operator is inside a replicator."""
        while oper:
            if oper.family == 'COMP' and oper.replicator:
                return True
            oper = oper.parent()
        return False

    # ==========================================================================
    # SAVE & DIRTY HANDLING
    # ==========================================================================

    def Save(self, opPath: str) -> None:
        """Save a TOX-strategy COMP and update tracking."""
        try:
            oper = op(opPath)
            oper.par.enableexternaltox = True

            # Update build info
            if hasattr(oper.par, 'Build'):
                new_build = oper.par.Build.val + 1
                oper.par.Build = new_build
                self.Externalizations[opPath, 'build'] = str(new_build)

            if hasattr(oper.par, 'Date'):
                oper.par.Date.val = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            if hasattr(oper.par, 'Touchbuild'):
                oper.par.Touchbuild = app.build
                self.Externalizations[opPath, 'touch_build'] = app.build

            oper.saveExternalTox()

            # Update timestamp
            if hasattr(oper.par, 'externalTimeStamp') and oper.externalTimeStamp != 0:
                utc_time = datetime.utcfromtimestamp(oper.externalTimeStamp / 10000000 - 11644473600)
                timestamp = utc_time.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            self.Externalizations[opPath, 'timestamp'] = timestamp
            self.param_tracker.updateParamStore(oper)
            self.Externalizations[opPath, 'dirty'] = False
            # Refresh position/color metadata
            self._updatePositionInTable(oper, opPath)

            self.Log(f"Saved {opPath}", "SUCCESS")
        except Exception as e:
            self.Log("Save failed", "ERROR", str(e))

    def SaveTDN(self, opPath: str) -> None:
        """Save a TDN-strategy COMP by re-exporting its .tdn file."""
        try:
            oper = op(opPath)
            if not oper:
                self.Log(f"Operator not found: {opPath}", "ERROR")
                return

            # Get the TDN file path from the table
            rel_path = self._getStrategyFilePath(opPath, 'tdn')
            if not rel_path:
                self.Log(f"No TDN entry found for {opPath}", "ERROR")
                return

            # For root /, re-derive filename from current project name
            # so it stays in sync when the .toe is renamed/versioned
            if opPath == '/':
                from pathlib import Path
                safe_name = project.name.removesuffix('.toe')
                ext_folder = self.ExternalizationsFolder or ''
                new_rel = self.normalizePath(
                    str(Path(ext_folder) / f'{safe_name}.tdn'))
                if new_rel != rel_path:
                    old_abs = self.buildAbsolutePath(rel_path)
                    if old_abs.is_file():
                        self.safeDeleteFile(str(old_abs))
                    rel_path = new_rel
                    self.Externalizations[opPath, 'rel_file_path'] = rel_path
                    self.Log(f"Updated root TDN path: {rel_path}", "INFO")

            # Update build info
            if hasattr(oper.par, 'Build'):
                new_build = oper.par.Build.val + 1
                oper.par.Build = new_build
                self.Externalizations[opPath, 'build'] = str(new_build)

            if hasattr(oper.par, 'Date'):
                oper.par.Date.val = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            if hasattr(oper.par, 'Touchbuild'):
                oper.par.Touchbuild = app.build
                self.Externalizations[opPath, 'touch_build'] = app.build

            # Export TDN — protect .tdn files belonging to OTHER tracked
            # TDN COMPs so the stale-file cleanup doesn't delete them.
            abs_path = str(self.buildAbsolutePath(rel_path))
            protected = self._getAllTrackedTDNFiles(exclude_path=opPath)
            result = self.my.ext.TDN.ExportNetwork(
                root_path=opPath, output_file=abs_path,
                cleanup_protected=protected)

            if result.get('success'):
                timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                self.Externalizations[opPath, 'timestamp'] = timestamp
                self.param_tracker.updateParamStore(oper)
                self.Externalizations[opPath, 'dirty'] = ''
                # Refresh position/color metadata
                self._updatePositionInTable(oper, opPath)
                # Snapshot the network structure so _isTDNDirty returns False
                self._storeTDNFingerprint(oper)
                self.Log(f"Exported TDN for {opPath}", "SUCCESS")
            else:
                self.Log(f"TDN export failed for {opPath}: {result.get('error')}", "ERROR")
        except Exception as e:
            self.Log(f"SaveTDN failed for {opPath}", "ERROR", str(e))

    def ExportPortableTox(self, target: 'OP' = None,
                          save_path: Optional[str] = None) -> bool:
        """Export a self-contained .tox with all external file references
        and Embody tags stripped.

        Temporarily strips file, syncfile, and externaltox parameters plus
        all Embody tags from all descendants of the target COMP, saves the
        .tox, then restores everything. The resulting .tox has no external
        file dependencies and no Embody metadata.

        Warns (but does not strip) about non-system absolute paths that won't
        be portable to other machines.

        Args:
            target: The COMP to export. Defaults to the Embody COMP itself.
            save_path: Absolute path for the output .tox. If None, uses the
                       default release path (release/{name}-v{version}.tox).

        Returns:
            True if the .tox was saved successfully, False otherwise.
        """
        if target is None:
            target = self.my
        if save_path is None:
            version = self.my.par.Version.eval()
            save_path = str(
                Path(project.folder).parents[0] / 'release'
                / f"{target.name}-v{version}.tox"
            )

        # Phase 1: Collect relative file references to strip, warn about
        # absolute paths that won't be portable.
        saved_state = []

        for child in target.findChildren():
            if child.family == 'DAT' and hasattr(child.par, 'file'):
                file_val = child.par.file.eval()
                if not file_val:
                    continue
                if file_val.startswith('/') or (len(file_val) > 1 and file_val[1] == ':'):
                    # Absolute path — warn if not a TD system path
                    if not file_val.startswith('/sys/'):
                        self.Log(
                            f"Absolute path won't be portable: "
                            f"{child.path} -> {file_val}", "WARNING")
                else:
                    saved_state.append({
                        'op': child,
                        'family': 'DAT',
                        'file': file_val,
                        'file_readonly': child.par.file.readOnly,
                        'syncfile': child.par.syncfile.eval(),
                    })

            elif child.family == 'COMP' and hasattr(child.par, 'externaltox'):
                tox_val = child.par.externaltox.eval()
                if not tox_val:
                    continue
                if tox_val.startswith('/') or (len(tox_val) > 1 and tox_val[1] == ':'):
                    if not tox_val.startswith('/sys/'):
                        self.Log(
                            f"Absolute path won't be portable: "
                            f"{child.path} -> {tox_val}", "WARNING")
                else:
                    saved_state.append({
                        'op': child,
                        'family': 'COMP',
                        'externaltox': tox_val,
                        'externaltox_readonly': child.par.externaltox.readOnly,
                        'enableexternaltox': child.par.enableexternaltox.eval(),
                    })

        # Phase 1b: Collect Embody tags to strip from all descendants
        # (including the target itself). Recipients don't need Embody
        # metadata — it would cause confusion if they have Embody installed.
        embody_tags = set(self.getTags())
        saved_tags = []  # list of (op_ref, set_of_removed_tags)

        # Check target itself, then all descendants
        for op_ref in [target] + target.findChildren():
            found = set(op_ref.tags) & embody_tags
            if found:
                saved_tags.append((op_ref, found))

        self.Log(
            f"Exporting portable .tox: stripping {len(saved_state)} "
            f"file reference(s) and {len(saved_tags)} tagged operator(s) "
            f"from {target.path}", "INFO")

        # Phase 2: Strip all collected relative references.
        for entry in saved_state:
            try:
                op_ref = entry['op']
                if entry['family'] == 'DAT':
                    op_ref.par.file.readOnly = False
                    op_ref.par.file = ''
                    op_ref.par.syncfile = False
                elif entry['family'] == 'COMP':
                    op_ref.par.externaltox.readOnly = False
                    op_ref.par.externaltox = ''
                    op_ref.par.enableexternaltox = False
            except Exception as e:
                self.Log(f"Failed to strip {entry['op'].path}: {e}", "WARNING")

        # Strip Embody tags.
        for op_ref, tags_to_remove in saved_tags:
            try:
                for tag in tags_to_remove:
                    op_ref.tags.remove(tag)
            except Exception as e:
                self.Log(
                    f"Failed to strip tags from {op_ref.path}: {e}", "WARNING")

        # Phase 3: Save the .tox.
        success = False
        try:
            target.save(str(save_path))
            try:
                rel_path = Path(save_path).relative_to(
                    Path(project.folder).parents[0])
            except ValueError:
                rel_path = save_path
            self.Log(f"Exported portable .tox: {rel_path}", "SUCCESS")
            success = True
        except Exception as e:
            self.Log(f"Portable .tox export failed: {e}", "ERROR")

        # Phase 4: Restore all references (always, even on failure).
        for entry in saved_state:
            try:
                op_ref = entry['op']
                if entry['family'] == 'DAT':
                    op_ref.par.file = entry['file']
                    op_ref.par.file.readOnly = entry['file_readonly']
                    op_ref.par.syncfile = entry['syncfile']
                elif entry['family'] == 'COMP':
                    op_ref.par.externaltox = entry['externaltox']
                    op_ref.par.externaltox.readOnly = entry['externaltox_readonly']
                    op_ref.par.enableexternaltox = entry['enableexternaltox']
            except Exception as e:
                self.Log(
                    f"Failed to restore {entry['op'].path}: {e}", "WARNING")

        # Restore Embody tags (always, even on save failure).
        for op_ref, tags_to_restore in saved_tags:
            try:
                for tag in tags_to_restore:
                    op_ref.tags.add(tag)
            except Exception as e:
                self.Log(
                    f"Failed to restore tags on {op_ref.path}: {e}", "WARNING")

        return success

    @staticmethod
    def _computeTDNFingerprint(comp) -> tuple:
        """Compute a hashable fingerprint of a TDN COMP's network structure.

        Used instead of oper.dirty for TDN COMPs (which always reads True
        because externaltox is empty). Captures all visual and metadata
        properties that a TDN export records: name, type, position, size,
        color, tags, flags, comment, connections, and annotations.
        """
        parts = []
        for c in sorted(comp.children, key=lambda c: c.name):
            # Skip annotations — they're fingerprinted separately below
            if c.type == 'annotate':
                continue
            color = tuple(round(v, 4) for v in c.color)
            tags = tuple(sorted(c.tags))
            flags = (c.bypass, c.lock, c.display, c.render,
                     c.viewer, c.current, c.expose)
            parts.append((
                c.name, c.type,
                c.nodeX, c.nodeY, c.nodeWidth, c.nodeHeight,
                color, tags, flags, c.comment,
            ))
            for i, conn in enumerate(c.inputConnectors):
                for link in conn.connections:
                    parts.append((c.name, 'in', i, link.owner.name))
        # All annotations (utility=True or False) — uses annotation-specific attrs
        for ann in sorted(comp.findChildren(type=annotateCOMP, depth=1,
                                            includeUtility=True),
                          key=lambda a: a.name):
            ann_color = tuple(round(v, 4) for v in (
                ann.par.Backcolorr.eval(), ann.par.Backcolorg.eval(),
                ann.par.Backcolorb.eval()))
            parts.append((
                ann.name, 'annotation',
                ann.par.Mode.eval(),
                ann.par.Titletext.eval(),
                ann.par.Bodytext.eval(),
                ann.nodeX, ann.nodeY, ann.nodeWidth, ann.nodeHeight,
                ann_color,
                round(ann.par.Opacity.eval(), 4),
            ))
        return tuple(parts)

    def _isTDNDirty(self, comp) -> bool:
        """Check if a TDN COMP's network has changed since last export."""
        current = self._computeTDNFingerprint(comp)
        stored = self._tdn_fingerprints.get(comp.path)
        if stored is None:
            # No stored fingerprint — assume clean (just initialized)
            self._tdn_fingerprints[comp.path] = current
            return False
        return current != stored

    def _storeTDNFingerprint(self, comp) -> None:
        """Snapshot the TDN COMP's network structure after export."""
        self._tdn_fingerprints[comp.path] = self._computeTDNFingerprint(comp)

    def _getStrategyFilePath(self, op_path: str, strategy: str) -> Optional[str]:
        """Return the rel_file_path for a given operator + strategy, or None."""
        table = self.Externalizations
        if not table:
            return None
        has_strategy_col = table[0, 'strategy'] is not None
        for i in range(1, table.numRows):
            if table[i, 'path'].val == op_path:
                if has_strategy_col and table[i, 'strategy'].val == strategy:
                    return table[i, 'rel_file_path'].val
                elif not has_strategy_col:
                    return table[i, 'rel_file_path'].val
        return None

    def _getAllTrackedTDNFiles(self, exclude_path: Optional[str] = None) -> list[str]:
        """Collect absolute paths of ALL tracked .tdn files in the table.

        Used to protect .tdn files belonging to other TDN COMPs from
        being deleted by stale-file cleanup during a single-COMP export.

        Args:
            exclude_path: Skip this op_path (the one being exported).
        """
        table = self.Externalizations
        if not table or table[0, 'strategy'] is None:
            return []
        protected = []
        for i in range(1, table.numRows):
            if table[i, 'strategy'].val != 'tdn':
                continue
            path = table[i, 'path'].val
            if path == exclude_path:
                continue
            rel = table[i, 'rel_file_path'].val
            if rel:
                protected.append(str(self.buildAbsolutePath(rel)))
        return protected

    def _getCompStrategy(self, comp: OP) -> Optional[str]:
        """Determine if a COMP uses 'tox' or 'tdn' strategy from the table."""
        table = self.Externalizations
        if not table:
            return None
        if table[0, 'strategy'] is None:
            return 'tox'  # Legacy table without strategy column
        for i in range(1, table.numRows):
            if table[i, 'path'].val == comp.path:
                s = table[i, 'strategy'].val
                if s in ('tox', 'tdn'):
                    return s
        return None

    def SaveCurrentComp(self) -> None:
        """Save only the COMP we're currently working inside of (Ctrl/Cmd+Alt+U)."""
        current_comp = None
        
        try:
            pane = ui.panes.current
            if pane and pane.owner:
                current_comp = pane.owner
        except Exception as e:
            self.Log(f"Failed to get current pane: {e}", "DEBUG")
            pass
        
        if not current_comp:
            self.Log("Could not determine current COMP", "WARNING")
            return
        
        # Check if this COMP is externalized
        comp_path = current_comp.path
        match = self._findExternalizedComp(comp_path)
        if match:
            self._saveByStrategy(*match)
            return

        # Check if any parent is externalized
        parent_comp = current_comp.parent()
        while parent_comp:
            match = self._findExternalizedComp(parent_comp.path)
            if match:
                self._saveByStrategy(*match)
                return
            parent_comp = parent_comp.parent()

        self.Log(f"No externalized COMP found at or above '{comp_path}'", "WARNING")

    def _findExternalizedComp(self, comp_path: str) -> Optional[tuple[str, str]]:
        """Find a COMP in the externalizations table and return (path, strategy)."""
        has_strategy_col = self.Externalizations[0, 'strategy'] is not None
        for i in range(1, self.Externalizations.numRows):
            if self.Externalizations[i, 'path'].val == comp_path:
                if has_strategy_col:
                    s = self.Externalizations[i, 'strategy'].val
                    if s in ('tox', 'tdn'):
                        return (comp_path, s)
                else:
                    return (comp_path, 'tox')
        return None

    def _saveByStrategy(self, op_path: str, strategy: str) -> None:
        """Save a COMP using the appropriate strategy."""
        if strategy == 'tdn':
            self.SaveTDN(op_path)
        else:
            self.Save(op_path)

    def dirtyHandler(self, update: bool) -> list[str]:
        """Check and optionally update dirty COMPs (both TOX and TDN)."""
        updates = []

        # TOX-strategy COMPs
        for oper in self.getExternalizedOps(COMP, strategy='tox'):
            dirty = oper.dirty
            try:
                # Preserve 'Par' dirty state when oper.dirty is False —
                # parameter changes are tracked independently from TD's
                # native dirty flag and should only be cleared on Save.
                if dirty or str(self.Externalizations[oper.path, 'dirty'].val) != 'Par':
                    self.Externalizations[oper.path, 'dirty'] = dirty
            except Exception as e:
                self.Log(f"Failed to update dirty state for {oper.path}: {e}", "DEBUG")
            if dirty and update:
                self.Save(oper.path)
                updates.append(oper.path)

        # TDN-strategy COMPs — use network fingerprint instead of oper.dirty
        # (oper.dirty is always True when externaltox is empty)
        for oper in self.getExternalizedOps(COMP, strategy='tdn'):
            dirty = self._isTDNDirty(oper)
            if dirty:
                try:
                    self.Externalizations[oper.path, 'dirty'] = 'True'
                except Exception as e:
                    self.Log(f"Failed to update dirty state for {oper.path}: {e}", "DEBUG")
                if update:
                    self.SaveTDN(oper.path)
                    updates.append(oper.path)

        return updates

    def updateDirtyStates(self, externalizationsFolder: str) -> None:
        """Update dirty states and check for path/parameter changes."""
        dirties = self.dirtyHandler(False)
        param_changes = []

        for oper in self.getExternalizedOps(COMP) + self.getExternalizedOps(DAT):
            # TDN-strategy COMPs don't use externaltox — their rel_file_path
            # is managed by _handleTDNAddition / _addToTable, not the par.
            # Skip them here to avoid overwriting the .tdn path with "".
            if oper.family == 'COMP' and self._getCompStrategy(oper) == 'tdn':
                if self.param_tracker.compareParameters(oper):
                    param_changes.append(oper.path)
                    self.Externalizations[oper.path, 'dirty'] = 'Par'
                continue

            current_path = self.getExternalPath(oper)
            try:
                table_path = self.normalizePath(self.Externalizations[oper.path, 'rel_file_path'].val)
                if current_path != table_path:
                    self.Externalizations[oper.path, 'rel_file_path'] = current_path
                    if oper.family == 'COMP':
                        oper.par.externaltox.readOnly = True
                    else:
                        oper.par.file.readOnly = True
                    self.Log(f"Updated path for {oper.path}", "SUCCESS")
            except Exception as e:
                self.Log(f"Failed to update path for {oper.path}: {e}", "WARNING")
                pass
            
            if oper.family == 'COMP' and self.param_tracker.compareParameters(oper):
                param_changes.append(oper.path)
                self.Externalizations[oper.path, 'dirty'] = 'Par'

        if dirties or param_changes:
            msgs = []
            if dirties:
                msgs.append(f"{len(dirties)} unsaved tox{'es' if len(dirties) > 1 else ''}")
            if param_changes:
                msgs.append(f"{len(param_changes)} COMP{'s' if len(param_changes) > 1 else ''} with param changes")
            self.Log(f"Found {' and '.join(msgs)}", "INFO")

    # ==========================================================================
    # ADDITION / SUBTRACTION HANDLING
    # ==========================================================================

    def handleAddition(self, oper: OP) -> None:
        """Process a newly tagged operator for externalization."""
        # Route TDN-tagged COMPs to the TDN handler
        if oper.family == 'COMP' and self.my.par.Tdntag.val in oper.tags:
            self._handleTDNAddition(oper)
            return

        abs_folder_path, save_file_path, rel_directory, rel_file_path = \
            self.getOpPaths(oper, self.my.par.Folder.val)

        if save_file_path is None:
            self.Log(f"Could not generate paths for {oper.path}", "ERROR")
            return

        # Create directory
        try:
            Path(abs_folder_path).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.Log(f"Error creating directory {abs_folder_path}", "ERROR", str(e))

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        dirty = ''
        build_num = ''
        touch_build = ''
        strategy = ''

        if oper.family == 'COMP':
            strategy = 'tox'
            self._setupCompForExternalization(oper, rel_file_path, save_file_path)
            dirty = oper.dirty
            build_num = int(oper.par.Build.eval()) if hasattr(oper.par, 'Build') else 1
            touch_build = str(oper.par.Touchbuild.eval()) if hasattr(oper.par, 'Touchbuild') else app.build
            self.param_tracker.updateParamStore(oper)
        else:  # DAT
            ext = str(save_file_path).rsplit('.', 1)[-1] if '.' in str(save_file_path) else ''
            strategy = ext
            self._setupDatForExternalization(oper, rel_file_path, save_file_path)

        # Add to table
        self._addToTable(oper, rel_file_path, timestamp, dirty, build_num, touch_build, strategy)
        self.Log(f"Added '{oper.path}'", "SUCCESS")

    def _handleTDNAddition(self, oper: OP) -> None:
        """Process a newly TDN-tagged COMP for externalization."""
        rel_path = self._buildTDNRelPath(oper)
        abs_path = self.buildAbsolutePath(rel_path)

        # Create directory
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.Log(f"Error creating directory {abs_path.parent}", "ERROR", str(e))

        # Setup build parameters
        build_page = next((p for p in oper.customPages if p.name == 'About'), None)
        if not build_page:
            build_page = oper.appendCustomPage('About')

        current_build = 1
        if hasattr(oper.par, 'Build'):
            current_build = oper.par.Build.eval()
        self.setupBuildParameters(oper, build_page, current_build, app.build)

        # Export TDN — protect .tdn files belonging to OTHER tracked
        # TDN COMPs so the stale-file cleanup doesn't delete them.
        # Without this, bottom-up addition order causes parent exports
        # to delete children's .tdn files as "stale".
        protected = self._getAllTrackedTDNFiles(exclude_path=oper.path)
        result = self.my.ext.TDN.ExportNetwork(
            root_path=oper.path, output_file=str(abs_path),
            cleanup_protected=protected)

        if result.get('success'):
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            build_num = int(oper.par.Build.eval()) if hasattr(oper.par, 'Build') else 1
            touch_build = str(oper.par.Touchbuild.eval()) if hasattr(oper.par, 'Touchbuild') else app.build
            self.param_tracker.updateParamStore(oper)
            self._addToTable(oper, str(rel_path), timestamp, False,
                             build_num, touch_build, 'tdn')
            self.Log(f"Added TDN '{oper.path}'", "SUCCESS")
        else:
            self.Log(f"TDN export failed for {oper.path}: {result.get('error')}", "ERROR")

    def _buildTDNRelPath(self, oper: OP) -> Path:
        """Generate a relative .tdn file path for a COMP."""
        ext_folder = self.ExternalizationsFolder
        parent_path = str(oper.parent().path).strip('/')
        parts = [p for p in parent_path.split('/') if p]

        path_parts = []
        if ext_folder:
            path_parts.append(ext_folder)
        path_parts.extend(parts)

        filename = oper.name + '.tdn'
        if path_parts:
            return Path('/'.join(path_parts)) / filename
        return Path(filename)

    def _setupCompForExternalization(self, oper, rel_file_path, save_file_path):
        """Configure a COMP for TOX externalization."""
        # Setup build info page
        build_page = next((p for p in oper.customPages if p.name == 'Build Info'), None)
        if not build_page:
            build_page = oper.appendCustomPage('About')
        
        current_build = 1
        if hasattr(oper.par, 'Build'):
            current_build = oper.par.Build.eval()
        else:
            for row in range(1, self.Externalizations.numRows):
                if self.Externalizations[row, 'path'].val == oper.path:
                    try:
                        current_build = int(self.Externalizations[row, 'build'].val)
                    except (ValueError, TypeError) as e:
                        self.Log(f"Failed to parse build number for {oper.path}: {e}", "DEBUG")
                        pass
                    break
        
        self.setupBuildParameters(oper, build_page, current_build, app.build)
        
        # Set external path
        if not oper.par.externaltox.eval():
            oper.par.externaltox = rel_file_path
        else:
            oper.par.externaltox = self.normalizePath(oper.par.externaltox.eval())
        
        oper.par.externaltox.readOnly = True
        oper.par.enableexternaltox = True
        
        # Save file
        save_path_str = str(save_file_path)
        try:
            oper.save(save_path_str)
        except Exception as e:
            self.Log(f"Failed to save COMP {oper.path}", "ERROR", f"Path: {save_path_str}, Error: {e}")

        if "Cannot load external tox from path" in oper.scriptErrors():
            oper.allowCooking = False
            run(lambda: self._safeAllowCooking(str(oper), True), delayFrames=1)

    def _setupDatForExternalization(self, oper, rel_file_path, save_file_path):
        """Configure a DAT for externalization."""
        if not oper.par.file.eval():
            oper.par.file = str(rel_file_path)
        else:
            oper.par.file = self.normalizePath(oper.par.file.eval())
        
        oper.par.syncfile = True
        op_path = str(oper)
        run(lambda: self._safeSyncFile(op_path, False), delayFrames=1)
        run(lambda: self._safeSyncFile(op_path, True), delayFrames=2)
        oper.par.file.readOnly = True
        
        save_path_str = str(save_file_path)
        try:
            oper.save(save_path_str)
        except Exception as e:
            self.Log(f"Failed to save DAT {oper.path}", "ERROR", f"Path: {save_path_str}, Error: {e}")

    def _addToTable(self, oper, rel_file_path, timestamp, dirty,
                     build_num, touch_build, strategy: str = ''):
        """Add or update operator entry in externalizations table."""
        normalized_path = self.normalizePath(rel_file_path)

        has_strategy_col = self.Externalizations[0, 'strategy'] is not None
        has_position_cols = self.Externalizations[0, 'node_x'] is not None

        # Build position/color strings from the operator
        node_x = str(int(oper.nodeX)) if has_position_cols else ''
        node_y = str(int(oper.nodeY)) if has_position_cols else ''
        node_color = ''
        if has_position_cols:
            c = oper.color
            node_color = f'{c[0]:.4f},{c[1]:.4f},{c[2]:.4f}'

        # Check if row already exists for this operator + strategy
        for row in range(1, self.Externalizations.numRows):
            if self.Externalizations[row, 'path'] == oper.path:
                if has_strategy_col:
                    row_strategy = self.Externalizations[row, 'strategy'].val
                    if row_strategy != strategy:
                        continue
                self.Externalizations[row, 'rel_file_path'] = normalized_path
                # Update position/color on existing rows too
                if has_position_cols:
                    self.Externalizations[row, 'node_x'] = node_x
                    self.Externalizations[row, 'node_y'] = node_y
                    self.Externalizations[row, 'node_color'] = node_color
                return

        # Add new row
        if has_strategy_col:
            row_data = [
                oper.path, oper.type, strategy, normalized_path, timestamp,
                dirty, build_num, touch_build
            ]
            if has_position_cols:
                row_data.extend([node_x, node_y, node_color])
            self.Externalizations.appendRow(row_data)
        else:
            self.Externalizations.appendRow([
                oper.path, oper.type, normalized_path, timestamp,
                dirty, build_num, touch_build
            ])

    def _updatePositionInTable(self, oper: 'OP', op_path: str) -> None:
        """Update position/color metadata for an operator in the table."""
        if self.Externalizations[0, 'node_x'] is None:
            return
        self.Externalizations[op_path, 'node_x'] = str(int(oper.nodeX))
        self.Externalizations[op_path, 'node_y'] = str(int(oper.nodeY))
        c = oper.color
        self.Externalizations[op_path, 'node_color'] = (
            f'{c[0]:.4f},{c[1]:.4f},{c[2]:.4f}')

    def handleSubtraction(self, oper: OP) -> None:
        """Process removal of an operator from externalization."""
        self.Externalizations.deleteRow(oper.path)
        if oper.family == 'COMP':
            oper.par.externaltox.readOnly = False
        elif oper.family == 'DAT':
            oper.par.file.readOnly = False
        self.Log(f"Removed '{oper.path}'", "SUCCESS")

    def setupBuildParameters(self, oper: COMP, build_page: Any, build_num: int, touch_build: Union[str, int]) -> None:
        """Setup build tracking parameters on a COMP."""
        # Build Number
        build_par = next((p for p in oper.customPars if p.name == 'Build'), None)
        if not build_par:
            build_par = build_page.appendInt('Build', label='Build Number')
            build_par.readOnly = True
        build_par.val = build_num
        
        # Date
        date_par = next((p for p in oper.customPars if p.name == 'Date'), None)
        if not date_par:
            date_par = build_page.appendStr('Date', label='Build Date')
            date_par.readOnly = True
        date_par.val = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        # Touch Build
        touch_par = next((p for p in oper.customPars if p.name == 'Touchbuild'), None)
        if not touch_par:
            touch_par = build_page.appendStr('Touchbuild', label='Touch Build')
            touch_par.readOnly = True
        touch_par.val = touch_build

    # ==========================================================================
    # CONTINUITY & RENAME HANDLING
    # ==========================================================================

    def checkOpsForContinuity(self, externalizationsFolder: str) -> None:
        """Check for renamed, moved, or missing operators and update accordingly."""
        self._checkExternalToxPar()

        try:
            rows_to_check = []
            tdn_comp_paths = set()
            headers = [self.Externalizations[0, c].val
                       for c in range(self.Externalizations.numCols)]
            has_strategy = 'strategy' in headers
            for i in range(1, self.Externalizations.numRows):
                row_path = self.Externalizations[i, 'path'].val
                if row_path:
                    rel_file_path = self.normalizePath(self.Externalizations[i, 'rel_file_path'].val)
                    row_type = self.Externalizations[i, 'type'].val
                    strategy = (self.Externalizations[i, 'strategy'].val
                                if has_strategy else '')
                    rows_to_check.append((row_path, rel_file_path, row_type, strategy))
                    # Collect TDN COMP paths so we can skip their children
                    is_tdn = (strategy == 'tdn') if has_strategy else (row_type == 'tdn')
                    if is_tdn:
                        tdn_comp_paths.add(row_path)

            # Detect stripped or missing TDN COMPs:
            # - Stripped: exists but has no children (e.g., after save
            #   strip, or crash during the strip/restore cycle)
            # - Missing: COMP was deleted entirely (e.g., crash before
            #   post-save restore, or .toe opened without reconstruction)
            # Their children will be restored by ReconstructTDNComps(),
            # so we must skip ALL their entries (even individually-
            # externalized ones like .py files) to prevent false removals.
            stripped_tdn_paths = set()
            for tdn_path in tdn_comp_paths:
                tdn_op = op(tdn_path)
                if not tdn_op or not tdn_op.findChildren(depth=1):
                    stripped_tdn_paths.add(tdn_path)

            # Check for ancestor rename before per-operator processing.
            # When a parent COMP is renamed, all children go missing
            # simultaneously — handle as a single batch operation.
            ancestor_result = self._detectAncestorRename(rows_to_check)
            if ancestor_result:
                old_prefix, new_prefix = ancestor_result
                self._handleAncestorRename(
                    old_prefix, new_prefix, rows_to_check,
                    externalizationsFolder)
                return

            processed_ops = set()

            for old_op_path, rel_file_path, row_type, strategy in rows_to_check:
                if old_op_path in processed_ops:
                    continue

                # TDN-strategy COMPs don't set externaltox/file — just verify the op exists
                is_tdn = (strategy == 'tdn') if has_strategy else (row_type == 'tdn')
                if is_tdn:
                    if not op(old_op_path):
                        # Check if .tdn file exists on disk — if so, the COMP
                        # can be reconstructed (e.g., after strip/crash) and
                        # we must NOT remove the tracking entry or delete the file.
                        if rel_file_path:
                            abs_tdn = self.buildAbsolutePath(
                                self.normalizePath(rel_file_path))
                            if abs_tdn.is_file():
                                continue  # Recoverable — skip
                        # Try to find the renamed COMP before removing
                        found = self._findMovedTDNOp(
                            old_op_path, rel_file_path, processed_ops)
                        if not found:
                            self.Log(f"Operator for TDN entry '{old_op_path}' no longer exists", "WARNING")
                            self._removeTDNStrategy(old_op_path)
                    continue

                # Skip operators inside TDN-strategy COMPs when appropriate:
                # - Always skip if no individual strategy (purely TDN-managed)
                # - Also skip if the parent TDN COMP is stripped (no children,
                #   e.g., crash recovery) — ReconstructTDNComps() will restore
                #   them, so checking now would cause false removals
                parent_tdn = next(
                    (p for p in tdn_comp_paths
                     if old_op_path.startswith(p + '/')), None)
                if parent_tdn is not None:
                    if not strategy or parent_tdn in stripped_tdn_paths:
                        continue

                existing_op = op(old_op_path)

                if existing_op:
                    # Verify this is actually the SAME operator (not a different one at same path)
                    # by checking if externaltox matches what we expect
                    current_ext_path = self.getExternalPath(existing_op)

                    if current_ext_path == rel_file_path:
                        # Same operator, just update timestamp
                        self._updateOpTimestamp(existing_op)
                    else:
                        # Different operator at this path! The original was likely moved.
                        # Search for the moved operator
                        found_moved = self._findMovedOp(
                            old_op_path, rel_file_path, externalizationsFolder, processed_ops
                        )
                        if not found_moved:
                            # Operator was replaced, not moved - remove old entry
                            self.Log(f"Operator at '{old_op_path}' was replaced", "WARNING")
                            self._handleMissingOperator(old_op_path, rel_file_path)
                else:
                    # Operator no longer exists at path - check for rename/move
                    found_renamed = self._findMovedOp(
                        old_op_path, rel_file_path, externalizationsFolder, processed_ops
                    )
                    if not found_renamed:
                        self._handleMissingOperator(old_op_path, rel_file_path)

        except Exception as e:
            self.Log("Error in checkOpsForContinuity", "ERROR", str(e))

    def _checkExternalToxPar(self):
        """Check for COMPs using deprecated fileFolder pattern."""
        comps_with_filefolder = self.root.findChildren(
            type=COMP,
            key=lambda x: (
                x.par.externaltox.expr and
                "me.parent().fileFolder + '/' +" in x.par.externaltox.expr
            )
        )
        
        if comps_with_filefolder:
            message = "Found COMPs using deprecated 'me.parent().fileFolder':\n\n"
            message += "\n".join([f"- {comp.path}" for comp in comps_with_filefolder])
            message += "\n\nReset these paths?"
            
            if ui.messageBox('Embody', message, buttons=['No', 'Yes']) == 1:
                for comp in comps_with_filefolder:
                    try:
                        comp.par.externaltox.expr = ''
                        comp.par.externaltox = ''
                        self.Log(f"Reset externaltox for '{comp.path}'", "SUCCESS")
                    except Exception as e:
                        self.Log(f"Error resetting '{comp.path}'", "ERROR", str(e))

    def _updateOpTimestamp(self, oper):
        """Update timestamp for an operator from file system."""
        if oper.family != 'COMP':
            return
            
        save_file_path = self.getOpPaths(oper, self.ExternalizationsFolder)[1]
        try:
            last_modified = int(Path(save_file_path).stat().st_mtime)
            last_modified_utc = datetime.utcfromtimestamp(last_modified)
            formatted_time = last_modified_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
            self.Externalizations[oper.path, 'timestamp'] = formatted_time
        except FileNotFoundError:
            self.Log(f"File not found for timestamp: {save_file_path}", "WARNING")
        except Exception as e:
            self.Log(f"Error updating timestamp for {oper.path}", "ERROR", str(e))

    def _findMovedOp(self, old_op_path, rel_file_path, externalizationsFolder, processed_ops):
        """Find if an operator was renamed or moved by checking file paths across all COMPs/DATs."""
        # Search all COMPs for one with matching externaltox
        for potential_op in self.root.findChildren(type=COMP):
            potential_path = self.normalizePath(potential_op.par.externaltox.eval()) if potential_op.par.externaltox else ''
            if potential_path and potential_path == rel_file_path and potential_op.path != old_op_path:
                self.Log(f"Found moved/renamed COMP: {old_op_path} -> {potential_op.path}", "INFO")
                self.updateMovedOp(potential_op, old_op_path, rel_file_path, externalizationsFolder)
                processed_ops.add(potential_op.path)
                return True
        
        # Search all DATs for one with matching file path
        for potential_op in self.root.findChildren(type=DAT):
            if not hasattr(potential_op.par, 'file'):
                continue
            potential_path = self.normalizePath(potential_op.par.file.eval()) if potential_op.par.file else ''
            if potential_path and potential_path == rel_file_path and potential_op.path != old_op_path:
                self.Log(f"Found moved/renamed DAT: {old_op_path} -> {potential_op.path}", "INFO")
                self.updateMovedOp(potential_op, old_op_path, rel_file_path, externalizationsFolder)
                processed_ops.add(potential_op.path)
                return True
        
        return False

    def _findMovedTDNOp(self, old_op_path: str, old_rel_file_path: str,
                        processed_ops: set) -> bool:
        """Find a TDN-strategy COMP that was renamed or moved.

        TDN COMPs don't use externaltox/file, so _findMovedOp can't find
        them. Instead, search for COMPs with the TDN tag that aren't
        tracked in the externalizations table.

        To avoid false matches, only same-parent candidates are considered
        and only when there is exactly one unambiguous candidate.
        """
        tdn_tag = self.my.par.Tdntag.val
        table = self.Externalizations

        # Collect all TDN paths currently in the table (excluding the
        # missing entry itself, which is about to be updated or removed)
        tracked_tdn_paths = set()
        for i in range(1, table.numRows):
            if table[i, 'strategy'].val == 'tdn':
                p = table[i, 'path'].val
                if p != old_op_path:
                    tracked_tdn_paths.add(p)

        # Embody exclusion — same as _getTDNStrategyComps
        embody_path = self.my.path

        # Search for untracked TDN-tagged COMPs in the same parent
        old_parent = '/'.join(old_op_path.rstrip('/').rsplit('/', 1)[:-1]) or '/'
        candidates = []
        for potential_op in self.root.findChildren(type=COMP, tags=[tdn_tag]):
            if potential_op.path in tracked_tdn_paths:
                continue
            if potential_op.path in processed_ops:
                continue
            # Skip Embody and its descendants
            if (potential_op.path == embody_path
                    or embody_path.startswith(potential_op.path + '/')
                    or potential_op.path.startswith(embody_path + '/')):
                continue
            # Only consider candidates in the same parent network
            if str(potential_op.parent().path) == old_parent:
                candidates.append(potential_op)

        if len(candidates) != 1:
            if len(candidates) > 1:
                names = ', '.join(c.name for c in candidates)
                self.Log(
                    f"Multiple untracked TDN COMPs in {old_parent} — "
                    f"cannot determine which replaced '{old_op_path}': {names}",
                    "WARNING")
            return False

        new_op = candidates[0]
        self.Log(f"Found moved/renamed TDN COMP: {old_op_path} -> {new_op.path}", "INFO")
        self._updateMovedTDNOp(new_op, old_op_path, old_rel_file_path)
        processed_ops.add(new_op.path)
        return True

    def _updateMovedTDNOp(self, new_op: OP, old_op_path: str,
                          old_rel_file_path: str) -> None:
        """Update table and .tdn file when a TDN-strategy COMP is renamed."""
        try:
            table = self.Externalizations
            row_index = self.cleanupDuplicateRows(old_op_path)
            if row_index is None:
                self.Log(f"TDN row not found for '{old_op_path}'", "ERROR")
                return

            # Generate the new .tdn file path
            new_rel_path = str(self._buildTDNRelPath(new_op))

            # Rename the old .tdn file on disk
            old_abs = self.buildAbsolutePath(
                self.normalizePath(old_rel_file_path)).resolve()
            new_abs = self.buildAbsolutePath(
                self.normalizePath(new_rel_path)).resolve()

            if old_abs.is_file():
                try:
                    new_abs.parent.mkdir(parents=True, exist_ok=True)
                    old_abs.rename(new_abs)
                    self.Log(f"Renamed TDN file: {old_rel_file_path} -> {new_rel_path}", "SUCCESS")
                except Exception as e:
                    self.Log(f"Error renaming TDN file", "ERROR", str(e))
            else:
                # Old file missing — re-export instead
                result = self.my.ext.TDN.ExportNetwork(
                    root_path=new_op.path, output_file=str(new_abs))
                if result.get('success'):
                    self.Log(f"Re-exported TDN for renamed COMP: {new_rel_path}", "SUCCESS")
                else:
                    self.Log(f"TDN re-export failed: {result.get('error')}", "ERROR")

            # Clean up old empty directory
            old_folder = old_abs.parent
            try:
                old_folder.rmdir()
            except OSError:
                pass  # Not empty or doesn't exist

            # Update table row
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            table[row_index, 'path'] = new_op.path
            table[row_index, 'type'] = new_op.type
            table[row_index, 'rel_file_path'] = self.normalizePath(new_rel_path)
            table[row_index, 'timestamp'] = timestamp
            table.cook(force=True)

            # Update fingerprint tracking
            old_fp = self._tdn_fingerprints.pop(old_op_path, None)
            if old_fp is not None:
                self._tdn_fingerprints[new_op.path] = old_fp

            # Update parameter tracking
            self.param_tracker.removeComp(old_op_path)
            self.param_tracker.updateParamStore(new_op)

            # Update child entries (individually externalized DATs inside
            # this TDN COMP) whose paths shifted with the rename.
            self._updateTDNChildren(old_op_path, new_op.path)

            self.Log(f"Updated TDN entry: {old_op_path} -> {new_op.path}", "SUCCESS")

        except Exception as e:
            self.Log("Error in _updateMovedTDNOp", "ERROR", str(e))

    def _updateTDNChildren(self, old_prefix: str, new_prefix: str) -> None:
        """Update table entries for children when a TDN COMP is renamed.

        Individually externalized DATs inside a TDN COMP have their own
        table rows. When the parent COMP is renamed, their op paths and
        file paths shift. This method updates each child via updateMovedOp.
        """
        table = self.Externalizations
        old_prefix_slash = old_prefix + '/'
        children = []

        for i in range(1, table.numRows):
            child_path = table[i, 'path'].val
            if child_path.startswith(old_prefix_slash):
                children.append((
                    child_path,
                    table[i, 'rel_file_path'].val,
                ))

        for child_path, child_rel_file in children:
            suffix = child_path[len(old_prefix):]
            new_child_path = new_prefix + suffix
            new_child = op(new_child_path)

            if new_child:
                self.updateMovedOp(
                    new_child, child_path, child_rel_file,
                    self.ExternalizationsFolder)
            else:
                # Child no longer exists at expected new path — remove stale row
                self._handleMissingOperator(child_path, child_rel_file)

    def _detectAncestorRename(self, rows_to_check):
        """Detect if multiple missing operators share a common path prefix change.

        When a COMP that is an ancestor of many externalized operators is renamed
        (e.g., /embody → /myproject), all tracked operators under it go missing
        simultaneously. This method detects that pattern and returns the old and
        new prefix so the rename can be handled as a single batch operation
        instead of 50+ individual updateMovedOp calls.

        Returns:
            (old_prefix, new_prefix) if an ancestor rename is detected,
            or None for normal per-operator handling.
        """
        # 1. Separate missing ops from present ops
        missing = []
        present = []
        for old_path, rel_file, row_type, strategy in rows_to_check:
            if op(old_path):
                present.append(old_path)
            else:
                missing.append((old_path, rel_file, row_type, strategy))

        # Need 3+ missing ops to consider ancestor rename
        # (1-2 could be individual renames/deletes)
        if len(missing) < 3:
            return None

        # 2. Find common prefix of all missing paths
        missing_paths = [p for p, _, _, _ in missing]
        common = os.path.commonprefix(missing_paths)
        # Truncate to last '/' to get a complete path segment
        slash_pos = common.rfind('/')
        if slash_pos <= 0:
            return None
        ancestor_path = common[:slash_pos]

        # 3. Verify the ancestor COMP no longer exists at old path
        if op(ancestor_path):
            return None

        # 4. Find what it was renamed to by searching for one of the missing
        #    operators by its file parameter (same approach as _findMovedOp)
        sample_path, sample_file, sample_type, _ = missing[0]
        suffix = sample_path[len(ancestor_path):]

        new_op = None
        # Search COMPs by externaltox
        for candidate in self.root.findChildren(type=COMP):
            ext_path = (self.normalizePath(candidate.par.externaltox.eval())
                        if candidate.par.externaltox else '')
            if ext_path and ext_path == sample_file:
                new_op = candidate
                break
        # Search DATs by file parameter
        if not new_op:
            for candidate in self.root.findChildren(type=DAT):
                if not hasattr(candidate.par, 'file'):
                    continue
                file_path = (self.normalizePath(candidate.par.file.eval())
                             if candidate.par.file else '')
                if file_path and file_path == sample_file:
                    new_op = candidate
                    break

        if not new_op:
            return None

        # 5. Derive new prefix from found operator
        if not new_op.path.endswith(suffix):
            return None
        new_prefix = new_op.path[:-len(suffix)] if suffix else new_op.path

        # 6. Verify ALL missing ops exist at new_prefix + their suffix
        for old_path, _, _, _ in missing:
            old_suffix = old_path[len(ancestor_path):]
            expected_new = new_prefix + old_suffix
            if not op(expected_new):
                return None

        # 7. Verify no present ops are under the old prefix
        #    (if some ops under the prefix still exist, not a clean rename)
        for p in present:
            if p.startswith(ancestor_path + '/'):
                return None

        self.Log(f"Detected ancestor rename: {ancestor_path} → {new_prefix} "
                 f"({len(missing)} operators affected)", "INFO")
        return (ancestor_path, new_prefix)

    def _handleAncestorRename(self, old_prefix, new_prefix, rows_to_check,
                               externalizationsFolder):
        """Handle an ancestor COMP rename as a single batch operation.

        Instead of calling updateMovedOp() for each operator (which involves
        clearing/resetting file parameters and saving each file individually),
        this method:
        1. Prompts the user for confirmation
        2. Renames the directory on disk (single atomic operation)
        3. Batch-updates the externalizations table
        4. Updates file/externaltox parameters on all affected operators
        """
        old_dir_segment = old_prefix.strip('/')
        new_dir_segment = new_prefix.strip('/')

        # --- Phase A: Calculate what will change ---
        affected = []
        for old_path, rel_file, row_type, strategy in rows_to_check:
            if old_path.startswith(old_prefix + '/') or old_path == old_prefix:
                new_path = new_prefix + old_path[len(old_prefix):]
                if rel_file.startswith(old_dir_segment + '/'):
                    new_rel_file = new_dir_segment + rel_file[len(old_dir_segment):]
                elif rel_file == old_dir_segment:
                    new_rel_file = new_dir_segment
                else:
                    new_rel_file = rel_file
                affected.append((old_path, new_path, rel_file, new_rel_file,
                                row_type, strategy))

        if not affected:
            return

        # --- Phase B: Prompt user ---
        msg = (f"Detected rename: {old_prefix} → {new_prefix}\n\n"
               f"{len(affected)} externalized files will be moved:\n"
               f"  {old_dir_segment}/...  →  {new_dir_segment}/...\n\n"
               f"This will rename the folder on disk and update all tracking.\n"
               f"Cancel to leave files at their current location.")
        choice = ui.messageBox('Embody — Ancestor Rename Detected', msg,
                               buttons=['Cancel', 'Proceed'])
        if choice != 1:
            self.Log(f"Ancestor rename cancelled by user: "
                     f"{old_prefix} → {new_prefix}", "INFO")
            return

        # --- Phase C: Rename directory on disk ---
        project_folder = Path(project.folder)
        old_dir = project_folder / old_dir_segment
        new_dir = project_folder / new_dir_segment

        if not old_dir.exists():
            self.Log(f"Source directory not found: {old_dir}", "ERROR")
            ui.messageBox('Embody Error',
                          f'Source directory not found:\n{old_dir_segment}/',
                          buttons=['OK'])
            return

        if new_dir.exists():
            self.Log(f"Target directory already exists: {new_dir}", "ERROR")
            ui.messageBox('Embody Error',
                          f'Cannot rename: directory "{new_dir_segment}/" '
                          f'already exists.', buttons=['OK'])
            return

        try:
            old_dir.rename(new_dir)
            self.Log(f"Renamed directory: {old_dir_segment}/ → "
                     f"{new_dir_segment}/", "SUCCESS")
        except Exception as e:
            self.Log("Failed to rename directory", "ERROR", str(e))
            ui.messageBox('Embody Error',
                          f'Failed to rename directory:\n{e}', buttons=['OK'])
            return

        # --- Phase D: Update externalizations table ---
        table = self.Externalizations
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        for old_path, new_path, old_rel, new_rel, _, _ in affected:
            for i in range(1, table.numRows):
                if table[i, 'path'].val == old_path:
                    table[i, 'path'] = new_path
                    table[i, 'rel_file_path'] = new_rel
                    table[i, 'timestamp'] = timestamp
                    break

        table.cook(force=True)

        # --- Phase E: Update operator file/externaltox parameters ---
        # Collect Embody's own DATs to defer their parameter updates
        embody_path = self.my.path
        deferred_updates = []

        for _, new_path, old_rel, new_rel, row_type, strategy in affected:
            target_op = op(new_path)
            if not target_op:
                self.Log(f"Operator not found at new path: {new_path}",
                         "WARNING")
                continue

            if strategy == 'tdn':
                continue

            try:
                if target_op.family == 'COMP':
                    current = (self.normalizePath(target_op.par.externaltox.eval())
                               if target_op.par.externaltox else '')
                    if current == old_rel:
                        # Defer Embody's own COMP to avoid self-reinit
                        if (new_path == embody_path or
                                new_path.startswith(embody_path + '/')):
                            deferred_updates.append(
                                (new_path, 'externaltox', new_rel))
                        else:
                            target_op.par.externaltox.readOnly = False
                            target_op.par.externaltox = new_rel
                            target_op.par.externaltox.readOnly = True
                elif hasattr(target_op.par, 'file'):
                    current = (self.normalizePath(target_op.par.file.eval())
                               if target_op.par.file else '')
                    if current == old_rel:
                        # Defer Embody's own DATs to avoid reinit mid-method
                        if new_path.startswith(embody_path + '/'):
                            deferred_updates.append(
                                (new_path, 'file', new_rel))
                        else:
                            target_op.par.file.readOnly = False
                            target_op.par.file = new_rel
                            target_op.par.file.readOnly = True
            except Exception as e:
                self.Log(f"Failed to update file param for {new_path}",
                         "WARNING", str(e))

        # --- Phase F: Update Folder parameter if needed ---
        folder_val = self.my.par.Folder.eval()
        if folder_val and folder_val.startswith(old_dir_segment):
            new_folder = new_dir_segment + folder_val[len(old_dir_segment):]
            self.my.par.Folder = new_folder

        # --- Phase G: Update param tracker and TDN fingerprints ---
        for old_path, new_path, _, _, _, strategy in affected:
            self.param_tracker.removeComp(old_path)
            target_op = op(new_path)
            if target_op:
                self.param_tracker.updateParamStore(target_op)
            # Move TDN fingerprints to new paths
            if strategy == 'tdn':
                old_fp = self._tdn_fingerprints.pop(old_path, None)
                if old_fp is not None:
                    self._tdn_fingerprints[new_path] = old_fp

        self.Log(f"Ancestor rename complete: {old_prefix} → {new_prefix} "
                 f"({len(affected)} operators updated)", "SUCCESS")

        # --- Phase H: Deferred updates for Embody's own operators ---
        # These are applied after this method returns to avoid extension
        # reinitialization while we're still executing.
        if deferred_updates:
            for op_path, par_name, new_val in deferred_updates:
                run(f"o = op('{op_path}'); "
                    f"o.par.{par_name}.readOnly = False; "
                    f"o.par.{par_name} = '{new_val}'; "
                    f"o.par.{par_name}.readOnly = True",
                    delayFrames=1)
            self.Log(f"Deferred {len(deferred_updates)} file param updates "
                     f"for Embody components", "DEBUG")

    def _handleMissingOperator(self, old_op_path, old_rel_file_path):
        """Handle an operator that no longer exists."""
        self.cleanupDuplicateRows(old_op_path)

        # Truly missing - remove the specific row from the table
        self.Log(f"Operator '{old_op_path}' no longer exists!", "WARNING")
        normalized = self.normalizePath(old_rel_file_path)
        for i in range(1, self.Externalizations.numRows):
            if (self.Externalizations[i, 'path'].val == old_op_path
                    and self.normalizePath(self.Externalizations[i, 'rel_file_path'].val) == normalized):
                self.RemoveListerRow(old_op_path, old_rel_file_path)
                break

    def updateMovedOp(self, new_op: OP, old_op_path: str, old_rel_file_path: str, externalizationsFolder: str) -> None:
        """Update table and files when an operator is renamed."""
        try:
            # Cleanup duplicates
            for i in range(1, self.Externalizations.numRows):
                if self.Externalizations[i, 'path'].val == new_op.path:
                    self.cleanupDuplicateRows(new_op.path)
                    break

            row_index = self.cleanupDuplicateRows(old_op_path)
            if row_index is None:
                self.Log(f"Row not found for '{old_op_path}'", "ERROR")
                return

            # Clear external path
            self.setExternalPath(new_op, '', readonly=False)

            # Generate new paths
            abs_folder_path, save_file_path, _, new_rel_file_path = \
                self.getOpPaths(new_op, externalizationsFolder)

            abs_folder_path.mkdir(parents=True, exist_ok=True)

            # Remove old file (SAFELY - this file is tracked)
            self._removeOldFile(old_rel_file_path)

            # Save to new location
            try:
                new_op.save(str(save_file_path))
                self.Log(f"Saved new file: {new_rel_file_path}", "SUCCESS")
            except Exception as e:
                self.Log(f"Error saving: {new_rel_file_path}", "ERROR", str(e))

            # Update operator
            self.setExternalPath(new_op, new_rel_file_path, readonly=True)
            if new_op.family == 'COMP':
                new_op.par.enableexternaltox = True
            else:
                new_op.par.syncfile = True

            # Update table
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            self.Externalizations[row_index, 'path'] = new_op.path
            self.Externalizations[row_index, 'type'] = new_op.type
            self.Externalizations[row_index, 'rel_file_path'] = self.normalizePath(new_rel_file_path)
            self.Externalizations[row_index, 'timestamp'] = timestamp
            self._updatePositionInTable(new_op, new_op.path)
            self.Externalizations.cook(force=True)
            self.cleanupDuplicateRows(new_op.path)

            # Update parameter tracking: remove stale old path, baseline new path
            self.param_tracker.removeComp(old_op_path)
            self.param_tracker.updateParamStore(new_op)

            self.Log(f"Updated table row for '{new_op.path}'", "SUCCESS")

        except Exception as e:
            self.Log("Error in updateMovedOp", "ERROR", str(e))

    def _removeOldFile(self, old_rel_file_path):
        """
        Remove old externalized file and empty directories.
        SAFETY: This is only called for files we know are tracked (during rename operations).
        """
        normalized = self.normalizePath(old_rel_file_path)
        old_file = self.buildAbsolutePath(normalized)
        old_folder = old_file.parent
        
        if old_file.is_file():
            try:
                old_file.unlink()
                self.Log(f"Removed old file: {normalized}", "INFO")
                
                # Remove empty directories only (safe operation)
                try:
                    if old_folder.exists() and not any(old_folder.iterdir()):
                        old_folder.rmdir()

                        current_dir = old_folder.parent
                        while current_dir.exists() and current_dir != Path(project.folder):
                            if not any(current_dir.iterdir()):
                                current_dir.rmdir()
                                current_dir = current_dir.parent
                            else:
                                break
                except Exception as e:
                    self.Log(f"Error removing directories", "ERROR", str(e))
            except Exception as e:
                self.Log(f"Error removing file: {normalized}", "ERROR", str(e))

    # ==========================================================================
    # DUPLICATE HANDLING
    # ==========================================================================

    def cleanupAllDuplicateRows(self) -> None:
        """Remove all duplicate rows in the externalizations table."""
        paths = set()
        for i in range(1, self.Externalizations.numRows):
            path = self.Externalizations[i, 'path'].val
            if path:
                paths.add(path)
        for path in paths:
            self.cleanupDuplicateRows(path)

    def cleanupDuplicateRows(self, path: str) -> Optional[int]:
        """Remove duplicate rows for a path, keeping most recent per type.

        A COMP can legitimately have both a TOX row and a TDN row — these are
        different externalization types, not duplicates. Only rows with the
        same path AND same type are true duplicates.
        """
        type_groups = {}

        for i in range(1, self.Externalizations.numRows):
            if self.Externalizations[i, 'path'].val == path:
                row_type = self.Externalizations[i, 'type'].val
                if row_type not in type_groups:
                    type_groups[row_type] = {'indices': [], 'timestamps': []}
                type_groups[row_type]['indices'].append(i)
                try:
                    ts_str = self.Externalizations[i, 'timestamp'].val
                    timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC") if ts_str else datetime.min
                except (ValueError, TypeError) as e:
                    self.Log(f"Failed to parse timestamp for row {i}: {e}", "DEBUG")
                    timestamp = datetime.min
                type_groups[row_type]['timestamps'].append(timestamp)

        kept_row = None
        rows_to_delete = []

        for type_key, group in type_groups.items():
            indices = group['indices']
            timestamps = group['timestamps']
            if len(indices) <= 1:
                if indices:
                    kept_row = indices[0]
                continue
            most_recent = timestamps.index(max(timestamps))
            row_to_keep = indices[most_recent]
            kept_row = row_to_keep
            for i in indices:
                if i != row_to_keep:
                    rows_to_delete.append(i)

        for i in sorted(rows_to_delete, reverse=True):
            row_type = self.Externalizations[i, 'type'].val
            self.Externalizations.deleteRow(i)
            self.Log(f"Removed duplicate row {i} for {path} (type={row_type})", "INFO")

        return kept_row

    def checkForDuplicates(self) -> None:
        """Check for and handle duplicate external file paths."""
        embody_tags = self.getTags()
        external_paths = {}
        duplicate_ops = []

        # Check COMPs
        for oper in self.root.findChildren(type=COMP, parName='externaltox'):
            if not any(tag in oper.tags for tag in embody_tags):
                continue
            if self.isInsideClone(oper):
                continue
            
            path = self.normalizePath(oper.par.externaltox.eval())
            if path:
                if path in external_paths:
                    duplicate_ops.append((oper, external_paths[path]))
                else:
                    external_paths[path] = oper

        # Check DATs
        for oper in self.root.findChildren(type=DAT, parName='file'):
            if not any(tag in oper.tags for tag in embody_tags):
                continue
            if self.isInsideClone(oper):
                continue
            
            path = self.normalizePath(oper.par.file.eval())
            if path:
                if path in external_paths:
                    duplicate_ops.append((oper, external_paths[path]))
                else:
                    external_paths[path] = oper

        # Handle duplicates
        for new_op, existing_op in duplicate_ops:
            if 'clone' in new_op.tags:
                continue

            choice = ui.messageBox('Duplicate Path Detected',
                f"Duplicate path for {new_op.family} '{new_op.path}'.\n\n"
                "'Reference': Use same file (adds 'clone' tag)\n"
                "'Duplicate': Create new externalization",
                buttons=['Cancel', 'Reference', 'Duplicate'])

            if choice == 1:  # Reference
                self._handleDuplicateAsReference(new_op)
            elif choice == 2:  # Duplicate
                self._handleDuplicateAsNew(new_op)

    def _handleDuplicateAsReference(self, oper):
        """Mark duplicate as intentional clone reference."""
        oper.tags.add('clone')
        oper.color = (self.my.par.Clonetagcolorr, self.my.par.Clonetagcolorg, self.my.par.Clonetagcolorb)
        
        rel_file_path = self.getExternalPath(oper)
        
        # Add to table if not exists
        row_exists = any(
            self.Externalizations[row, 'path'] == oper.path
            for row in range(1, self.Externalizations.numRows)
        )
        
        if not row_exists:
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            build_num = int(oper.par.Build.eval()) if hasattr(oper.par, 'Build') else 1
            touch_build = str(oper.par.Touchbuild.eval()) if hasattr(oper.par, 'Touchbuild') else app.build
            
            self.Externalizations.appendRow([
                oper.path, oper.type, rel_file_path, timestamp, 
                '', build_num if oper.family == 'COMP' else '', 
                touch_build if oper.family == 'COMP' else ''
            ])
        
        self.Log(f"Added 'clone' tag to {oper.path}", "SUCCESS")

    def _handleDuplicateAsNew(self, oper):
        """Create new externalization for duplicate."""
        if oper.family == 'COMP':
            oper.par.externaltox = ''
        else:
            oper.par.file = ''
        self.Update()
        self.Log(f"Created new externalization for {oper.path}", "SUCCESS")

    # ==========================================================================
    # TAGGING UI
    # ==========================================================================

    def TagGetter(self) -> None:
        """Open tagging menu for rollover operator."""
        params = self.tagger.op('tags')
        switch = self.tagger.op('switch_family')
        oper = ui.rolloverOp
        self.rolloverOp = oper

        # Validation
        if oper is None:
            return

        if oper.type == 'engine':
            ui.messageBox('Embody Error', f"'{oper.type}' type not supported.", buttons=['Ok'])
            return

        if self.isReplicant(oper) or self.isClone(oper) or self.isInsideClone(oper):
            ui.messageBox('Embody Warning', 
                f"'{oper.path}' is a replicant or clone and cannot be externalized.", 
                buttons=['Ok'])
            return

        # Route based on family + tag state
        if oper.type in self.supported_dat_types:
            switch.par.index = 1
            active_tag = self._getActiveDATTag(oper)
            if active_tag:
                run(lambda: self.SetupTaggerDATManageMode(oper, active_tag), delayFrames=1)
                run(f"op('{self.tagging_menu_window}').par.winopen.pulse()", delayFrames=2)
                return
        elif oper.family == 'COMP':
            switch.par.index = 2
            tox_tag = self.my.par.Toxtag.val
            tdn_tag = self.my.par.Tdntag.val
            if tox_tag in oper.tags:
                run(lambda: self.SetupTaggerManageMode(oper, 'TOX_'), delayFrames=1)
                run(f"op('{self.tagging_menu_window}').par.winopen.pulse()", delayFrames=2)
                return
            elif tdn_tag in oper.tags:
                run(lambda: self.SetupTaggerManageMode(oper, 'TDN_'), delayFrames=1)
                run(f"op('{self.tagging_menu_window}').par.winopen.pulse()", delayFrames=2)
                return
        else:
            ui.messageBox('Embody Error',
                'Tags can only be applied to COMPs or supported DATs.',
                buttons=['Ok'])
            return

        # Untagged operator — show tag selection
        run(lambda: self.SetupTaggerTagMode(oper), delayFrames=1)
        run(f"op('{self.tagging_menu_window}').par.winopen.pulse()", delayFrames=2)

    def SetupTagger(self, oper: OP) -> None:
        """Configure tagger button colors."""
        params = self.tagger.op('tags')

        for i in range(1, params.numRows):
            button = self.tagger.op(f'button{i}')
            if button:
                button.par.colorr = self.my.par.Taggingmenucolorr
                button.par.colorg.expr = self._alternateColor('parent.Embody.par.Taggingmenucolorg')
                button.par.colorb = self.my.par.Taggingmenucolorb

    def _alternateColor(self, color_ref):
        """Generate alternating color expression."""
        return f'{color_ref} if me.digits % 2 else {color_ref} - 0.05'

    def SetupTaggerManageMode(self, oper: OP, strategy_state: str) -> None:
        """Configure tagger for manage mode on an already-tagged COMP.

        Shows Switch/Remove buttons for tox/tdn plus Save.
        """
        self._tagger_mode = 'manage'
        self.rolloverOp = oper

        # Ensure switch is set to COMP tags (tox/tdn only)
        switch = self.tagger.op('switch_family')
        if switch:
            switch.par.index = 2

        # Keep replicated tag buttons visible and highlight active tag
        self.SetupTagger(oper)

        # Set dynamic labels on tag buttons based on current strategy
        is_tox = strategy_state.startswith('TOX_')
        tox_btn = self.tagger.op('button1')
        tdn_btn = self.tagger.op('button2')
        if tox_btn:
            tox_btn.par.display = True
            tox_btn.par.label = '\u00d7  Remove tox' if is_tox else '\u21c4  Convert to tox'
        if tdn_btn:
            tdn_btn.par.display = True
            tdn_btn.par.label = '\u00d7  Remove tdn' if not is_tox else '\u21c4  Convert to tdn'

        # Hide any extra DAT-tag buttons (safety net for replicator timing)
        for i in range(3, 16):
            btn = self.tagger.op(f'button{i}')
            if btn:
                btn.par.display = False

        # Show Save button
        btn_save = self.tagger.op('btn_save')
        if btn_save:
            btn_save.par.display = True
            btn_save.par.label = '\u2193  Save tox' if is_tox else '\u2193  Save tdn'
            btn_save.par.colorr = self.my.par.Taggingmenucolorr.eval()
            btn_save.par.colorg = self.my.par.Taggingmenucolorg.eval()
            btn_save.par.colorb = self.my.par.Taggingmenucolorb.eval()

        # Show Reload button
        btn_reload = self.tagger.op('btn_reload')
        if btn_reload:
            btn_reload.par.display = True
            btn_reload.par.label = '\u21bb  Reload tox' if is_tox else '\u21bb  Reload tdn'
            btn_reload.par.colorr = self.my.par.Taggingmenucolorr.eval()
            btn_reload.par.colorg = self.my.par.Taggingmenucolorg.eval()
            btn_reload.par.colorb = self.my.par.Taggingmenucolorb.eval()

        # Show Embed DATs toggle (TDN COMPs only)
        btn_embed = self.tagger.op('btn_embed')
        embed_visible = not is_tox
        if btn_embed:
            btn_embed.par.display = embed_visible
            if embed_visible:
                per_comp = oper.fetch('embed_dats_in_tdn', None, search=False)
                effective = per_comp if per_comp is not None else self.my.par.Embeddatsintdns.eval()
                btn_embed.par.label = '\u229e  Embed DATs in tdn  \u2713' if effective else '\u229e  Embed DATs in tdn'
                btn_embed.par.colorr = self.my.par.Taggingmenucolorr.eval()
                btn_embed.par.colorg = self.my.par.Taggingmenucolorg.eval()
                btn_embed.par.colorb = self.my.par.Taggingmenucolorb.eval()

        # Show Export portable tox button
        btn_portable = self.tagger.op('btn_portable')
        if btn_portable:
            btn_portable.par.display = True
            btn_portable.par.label = '\u2197  Export portable tox'
            btn_portable.par.colorr = self.my.par.Taggingmenucolorr.eval()
            btn_portable.par.colorg = self.my.par.Taggingmenucolorg.eval()
            btn_portable.par.colorb = self.my.par.Taggingmenucolorb.eval()

        # Hide Remove button (use Remove tox/tdn buttons instead)
        btn_remove = self.tagger.op('btn_remove')
        if btn_remove:
            btn_remove.par.display = False

        # Show Open file button with platform-specific label
        btn_openfile = self.tagger.op('btn_openfile')
        strategy = 'tdn' if strategy_state.startswith('TDN') else 'tox'
        rel_fp = self._getStrategyFilePath(oper.path, strategy) or ''
        self.tagger.store('manage_file_path', rel_fp)
        if btn_openfile:
            btn_openfile.par.display = bool(rel_fp)
            label = '\u25ce  Reveal in Finder' if sys.platform.startswith('darwin') else '\u25ce  Reveal in Explorer'
            btn_openfile.par.label = label

        # Update header text
        title = self.tagger.op('header/text1')
        if title:
            title.par.text = 'Actions'

        # Update height: header + 2 tag buttons + Save + Reload + Export Portable
        # (+ Embed DATs for TDN) (+ Open file if applicable)
        visible_count = 6 + (1 if embed_visible else 0) + (1 if rel_fp else 0)
        self.tagger.store('visible_count', visible_count)

    def SetupTaggerDATManageMode(self, oper: OP, active_tag: str) -> None:
        """Configure tagger for manage mode on an already-tagged DAT.

        Shows Convert to <format> options, Remove, and Reveal in Finder.
        """
        self._tagger_mode = 'manage'
        self.rolloverOp = oper

        # Ensure switch is set to DAT tags
        switch = self.tagger.op('switch_family')
        if switch:
            switch.par.index = 1

        self.SetupTagger(oper)

        # COMP tags that should not appear as "Convert to" options for DATs
        comp_tags = {self.my.par.Toxtag.val, self.my.par.Tdntag.val}

        # Use replicated buttons for "Convert to <format>" options
        tags = self.tagger.op('tags')
        convert_count = 0
        for i in range(1, tags.numRows):
            btn = self.tagger.op(f'button{i}')
            if btn:
                tag_val = tags[i, 'value'].val
                if tag_val == active_tag or tag_val in comp_tags:
                    btn.par.display = False
                else:
                    btn.par.display = True
                    btn.par.label = f'\u21c4  Convert to {tag_val}'
                    convert_count += 1

        # Hide Save button (DATs use syncfile)
        btn_save = self.tagger.op('btn_save')
        if btn_save:
            btn_save.par.display = False

        # Show Remove button
        btn_remove = self.tagger.op('btn_remove')
        if btn_remove:
            btn_remove.par.display = True
            btn_remove.par.label = '\u00d7  Remove externalization'
            btn_remove.par.colorr = self.my.par.Taggingmenucolorr.eval()
            btn_remove.par.colorg = self.my.par.Taggingmenucolorg.eval()
            btn_remove.par.colorb = self.my.par.Taggingmenucolorb.eval()

        # Hide portable tox, reload, and embed (COMP-only actions)
        btn_portable = self.tagger.op('btn_portable')
        if btn_portable:
            btn_portable.par.display = False
        btn_reload = self.tagger.op('btn_reload')
        if btn_reload:
            btn_reload.par.display = False
        btn_embed = self.tagger.op('btn_embed')
        if btn_embed:
            btn_embed.par.display = False

        # Show Reveal in Finder/Explorer
        btn_openfile = self.tagger.op('btn_openfile')
        rel_fp = self.getExternalPath(oper)
        self.tagger.store('manage_file_path', rel_fp or '')
        if btn_openfile:
            btn_openfile.par.display = bool(rel_fp)
            label = '\u25ce  Reveal in Finder' if sys.platform.startswith('darwin') else '\u25ce  Reveal in Explorer'
            btn_openfile.par.label = label

        # Update header
        title = self.tagger.op('header/text1')
        if title:
            title.par.text = 'Actions'

        # Height: header + convert buttons + Remove + (Reveal if applicable)
        visible_count = 1 + convert_count + 1 + (1 if rel_fp else 0)
        self.tagger.store('visible_count', visible_count)

    def SetupTaggerTagMode(self, oper: OP) -> None:
        """Restore tagger to tag selection mode, then set up colors."""
        self._tagger_mode = 'tag'

        # Hide manage buttons
        btn_save = self.tagger.op('btn_save')
        btn_reload = self.tagger.op('btn_reload')
        btn_remove = self.tagger.op('btn_remove')
        btn_openfile = self.tagger.op('btn_openfile')
        btn_portable = self.tagger.op('btn_portable')
        btn_embed = self.tagger.op('btn_embed')
        if btn_save:
            btn_save.par.display = False
        if btn_reload:
            btn_reload.par.display = False
        if btn_embed:
            btn_embed.par.display = False
        if btn_remove:
            btn_remove.par.display = False
        if btn_openfile:
            btn_openfile.par.display = False
        if btn_portable:
            btn_portable.par.display = False

        # Find if operator already has an Embody tag
        tags = self.tagger.op('tags')
        existing_tag = None
        existing_tag_index = None
        for i in range(1, tags.numRows):
            tag_val = tags[i, 'value'].val
            if tag_val in oper.tags:
                existing_tag = tag_val
                existing_tag_index = i
                break

        # Mutual exclusivity: if already tagged, only show Remove for
        # the active tag. If untagged, show all Add options.
        visible_count = 0
        for i in range(1, tags.numRows):
            btn = self.tagger.op(f'button{i}')
            if btn:
                tag_val = tags[i, 'value'].val
                if existing_tag is not None:
                    if i == existing_tag_index:
                        btn.par.display = True
                        btn.par.label = f'\u00d7  Remove {tag_val}'
                        visible_count += 1
                    else:
                        btn.par.display = False
                else:
                    btn.par.display = True
                    btn.par.label = f'+  Add {tag_val}'
                    visible_count += 1

        # Restore header text
        title = self.tagger.op('header/text1')
        if title:
            title.par.text = 'Externalize'

        # Update height to match visible button count (+1 for header row)
        self.tagger.store('visible_count', visible_count + 1)

        # Delegate to existing color setup
        self.SetupTagger(oper)

    def TagSetter(self, oper: OP, tag: str) -> bool:
        """Toggle a tag on an operator. Enforces mutual exclusivity."""
        color = self._getTagColor(oper, tag)
        if color is None:
            return False

        if tag not in oper.tags:
            # Enforce mutual exclusivity: only one tag at a time
            if oper.family == 'COMP':
                tox_tag = self.my.par.Toxtag.val
                tdn_tag = self.my.par.Tdntag.val
                other_tag = tdn_tag if tag == tox_tag else tox_tag
                if other_tag in oper.tags:
                    self._removeCompStrategy(oper, other_tag)
            elif oper.family == 'DAT':
                # Remove any existing DAT tag before adding the new one
                dat_tags = self.getTags('DAT')
                for existing in list(oper.tags):
                    if existing in dat_tags:
                        oper.tags.remove(existing)
                        rel_file_path = self.getExternalPath(oper)
                        self.RemoveListerRow(oper.path, rel_file_path)
                        oper.par.file = ''
                        oper.par.file.readOnly = False
                        break

            oper.tags.add(tag)
            oper.color = color
            self._setDATLanguageForTag(oper, tag)
        else:
            oper.tags.remove(tag)
            self.resetOpColor(oper)

            if oper.family == 'COMP':
                if tag == self.my.par.Toxtag.val:
                    rel_file_path = self.getExternalPath(oper)
                    self.RemoveListerRow(oper.path, rel_file_path)
                    oper.par.externaltox = ''
                    oper.par.externaltox.readOnly = False
                elif tag == self.my.par.Tdntag.val:
                    self._removeTDNStrategy(oper.path)
            elif oper.family == 'DAT':
                rel_file_path = self.getExternalPath(oper)
                self.RemoveListerRow(oper.path, rel_file_path)
                oper.par.file = ''
                oper.par.file.readOnly = False

        return True

    def _removeCompStrategy(self, oper: OP, tag: str) -> None:
        """Remove a COMP strategy tag and clean up its externalization."""
        oper.tags.discard(tag)
        if tag == self.my.par.Toxtag.val:
            rel_file_path = self.getExternalPath(oper)
            self.RemoveListerRow(oper.path, rel_file_path)
            oper.par.externaltox = ''
            oper.par.externaltox.readOnly = False
        elif tag == self.my.par.Tdntag.val:
            self._removeTDNStrategy(oper.path)

    def _removeTDNStrategy(self, op_path: str) -> None:
        """Remove TDN strategy entry from table and delete .tdn file."""
        table = self.Externalizations
        if not table:
            return
        if table[0, 'strategy'] is None:
            return  # Legacy table without strategy column — no TDN entries
        for i in range(1, table.numRows):
            if (table[i, 'path'].val == op_path
                    and table[i, 'strategy'].val == 'tdn'):
                rel_path = table[i, 'rel_file_path'].val
                if rel_path:
                    full_path = self.buildAbsolutePath(
                        self.normalizePath(rel_path)).resolve()
                    def _delete(fp=full_path, rp=rel_path, opp=op_path):
                        try:
                            if fp.is_file() and fp.suffix.lower() == '.tdn':
                                fp.unlink()
                                self.Log(f'Removed TDN externalization for {opp} ({rp})', 'SUCCESS')
                        except Exception as e:
                            self.Log(f'Error removing TDN file: {e}', 'ERROR')
                    run(_delete, delayFrames=5)
                table.deleteRow(i)
                # Also remove orphaned child entries whose operators
                # no longer exist (the parent COMP was deleted/lost).
                self._removeOrphanedTDNChildren(op_path)
                return

    def _removeOrphanedTDNChildren(self, parent_path: str) -> None:
        """Remove table entries for children of a removed TDN COMP.

        Only removes entries where the operator no longer exists,
        preventing accidental deletion of valid entries.
        """
        table = self.Externalizations
        prefix = parent_path + '/'
        rows_to_delete = []

        for i in range(1, table.numRows):
            child_path = table[i, 'path'].val
            if child_path.startswith(prefix) and not op(child_path):
                rows_to_delete.append(i)

        # Delete in reverse order to preserve row indices
        for i in reversed(rows_to_delete):
            rel_file = table[i, 'rel_file_path'].val
            self.Log(f"Removed orphaned child entry: {table[i, 'path'].val}", "INFO")
            table.deleteRow(i)

    def _getTagColor(self, oper, tag):
        """Get appropriate color for tag on operator, or None if invalid."""
        if oper.family == 'COMP':
            if tag == self.my.par.Toxtag.val:
                return (self.my.par.Toxtagcolorr, self.my.par.Toxtagcolorg, self.my.par.Toxtagcolorb)
            elif tag == self.my.par.Tdntag.val:
                return (self.my.par.Tdntagcolorr, self.my.par.Tdntagcolorg, self.my.par.Tdntagcolorb)
            self.Log("Use TOX or TDN tag for COMPs", "ERROR")
            return None
        elif oper.family == 'DAT':
            if tag in self.getTags('DAT') and oper.type in self.supported_dat_types:
                return (self.my.par.Dattagcolorr, self.my.par.Dattagcolorg, self.my.par.Dattagcolorb)
            self.Log("DAT tags can only be applied to supported DAT types", "ERROR")
            return None

        self.Log("Tags can only be applied to COMPs or DATs", "ERROR")
        return None

    def _getActiveDATTag(self, oper: OP) -> Optional[str]:
        """Return the active Embody DAT tag on an operator, or None."""
        dat_tags = self.getTags('DAT')
        for tag in dat_tags:
            if tag in oper.tags:
                return tag
        return None

    def _inferDATTagValue(self, oper) -> str:
        """Infer the best externalization tag value for a DAT operator.
        Returns tag value string (e.g. 'py', 'txt', 'tsv') for applyTagToOperator().
        """
        if oper.type != 'text':
            tag_param = self.dat_type_to_tag.get(oper.type, 'Pytag')
            return getattr(self.my.par, tag_param).eval()

        lang = oper.par.language.eval() if hasattr(oper.par, 'language') else ''
        ext = oper.par.extension.eval() if hasattr(oper.par, 'extension') else ''
        tag_param = self.extension_to_tag.get(lang) or self.extension_to_tag.get(ext) or 'Pytag'
        return getattr(self.my.par, tag_param).eval()

    def _setDATLanguageForTag(self, oper, tag):
        """Set the language and/or extension on a text DAT to match the tag."""
        if oper.family != 'DAT' or oper.type != 'text':
            return
        lang = self.tag_to_language.get(tag)
        if lang:
            oper.par.language = lang
        ext = self.tag_to_extension.get(tag)
        if ext:
            oper.par.extension = ext

    def applyTagToOperator(self, oper: OP, tag: str) -> bool:
        """Apply a tag to an operator. Enforces mutual exclusivity."""
        color = self._getTagColor(oper, tag)
        if color is None:
            return False

        if tag not in oper.tags:
            # Enforce mutual exclusivity: only one tag at a time
            if oper.family == 'COMP':
                tox_tag = self.my.par.Toxtag.val
                tdn_tag = self.my.par.Tdntag.val
                other_tag = tdn_tag if tag == tox_tag else tox_tag
                if other_tag in oper.tags:
                    self._removeCompStrategy(oper, other_tag)
            elif oper.family == 'DAT':
                dat_tags = self.getTags('DAT')
                for existing in list(oper.tags):
                    if existing in dat_tags:
                        oper.tags.remove(existing)
                        rel_file_path = self.getExternalPath(oper)
                        self.RemoveListerRow(oper.path, rel_file_path)
                        oper.par.file = ''
                        oper.par.file.readOnly = False
                        self.Log(f"Removed existing '{existing}' tag from '{oper.path}' (replaced by '{tag}')", "INFO")
                        break

            oper.tags.add(tag)
            oper.color = color
            self._setDATLanguageForTag(oper, tag)
            self.Log(f"Tag '{tag}' applied to '{oper.path}'", "SUCCESS")

            if oper.family == 'COMP' and tag == self.my.par.Toxtag.val:
                if oper.par.externaltox.eval():
                    rel_file_path = self.normalizePath(oper.par.externaltox.eval())
                    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                    self.Externalizations.appendRow([
                        oper.path, oper.type, 'tox', rel_file_path,
                        timestamp, oper.dirty, '', ''
                    ])
                    self.Log(f"Added existing TOX externalization to table", "SUCCESS")
            elif oper.family == 'COMP' and tag == self.my.par.Tdntag.val:
                self._handleTDNAddition(oper)

        return True

    def TagExiter(self) -> None:
        """Close tagging menu and reset mode."""
        self._tagger_mode = 'tag'
        self.tagging_menu_window.par.winclose.pulse()
        self.my.op('list/list_callbacks').module.clearActiveStrategy()
        self.lister.reset()

    def HandleStrategySwitch(self, oper: OP) -> None:
        """Switch a COMP between TOX and TDN strategies."""
        tox_tag = self.my.par.Toxtag.val
        tdn_tag = self.my.par.Tdntag.val

        if tox_tag in oper.tags:
            self.applyTagToOperator(oper, tdn_tag)
        elif tdn_tag in oper.tags:
            self.applyTagToOperator(oper, tox_tag)

        self.ExternalizeImmediate(oper)
        self.Refresh()

    def HandleStrategySave(self, oper: OP) -> None:
        """Save the current strategy for a COMP."""
        tox_tag = self.my.par.Toxtag.val
        tdn_tag = self.my.par.Tdntag.val

        if tox_tag in oper.tags:
            self.Save(oper.path)
        elif tdn_tag in oper.tags:
            self.SaveTDN(oper.path)
        else:
            # Fallback: check externalizations table for untagged COMPs (e.g. root)
            strategy = self._getCompStrategy(oper)
            if strategy == 'tox':
                self.Save(oper.path)
            elif strategy == 'tdn':
                self.SaveTDN(oper.path)

        self.Refresh()

    def HandleReload(self, oper: OP) -> None:
        """Reload a COMP from its external tdn/tox file on disk."""
        tox_tag = self.my.par.Toxtag.val
        tdn_tag = self.my.par.Tdntag.val

        # Determine strategy from tags, falling back to table for untagged COMPs
        if tdn_tag in oper.tags:
            strategy = 'tdn'
        elif tox_tag in oper.tags:
            strategy = 'tox'
        else:
            strategy = self._getCompStrategy(oper) or 'tox'

        result = ui.messageBox(
            'Reload',
            f'Reload this {strategy.upper()} from disk?\n\n'
            'This will discard any unsaved in-memory changes\n'
            'and replace the contents with the file on disk.\n\n'
            'Operator: ' + oper.path,
            buttons=['Cancel', 'Reload'])

        if result != 1:
            return

        if strategy == 'tdn':
            self._reloadTDN(oper)
        else:
            self._reloadTox(oper)

        self.Refresh()

    def _reloadTDN(self, oper: OP) -> None:
        """Reload a single TDN-strategy COMP from its .tdn file on disk."""
        rel_tdn_path = self._getStrategyFilePath(oper.path, 'tdn')
        if not rel_tdn_path:
            self.Log(f'No TDN file path found for {oper.path}', 'ERROR')
            return

        abs_path = self.buildAbsolutePath(rel_tdn_path)
        if not abs_path.is_file():
            self.Log(f'TDN file not found: {rel_tdn_path}', 'ERROR')
            return

        try:
            import json
            tdn_doc = json.loads(abs_path.read_text(encoding='utf-8'))
        except Exception as e:
            self.Log(f'Failed to read TDN for {oper.path}: {e}', 'ERROR')
            return

        result = self.my.ext.TDN.ImportNetwork(
            target_path=oper.path,
            tdn=tdn_doc,
            clear_first=True,
            restore_file_links=True,
        )

        if result.get('error'):
            self.Log(f'Reload failed for {oper.path}: {result["error"]}', 'ERROR')
        else:
            created = result.get('created_count', 0)
            restored = result.get('restored_file_links', 0)
            msg = f'Reloaded {oper.path} from disk ({created} ops'
            if restored:
                msg += f', {restored} file links'
            msg += ')'
            self.Log(msg, 'SUCCESS')

    def _reloadTox(self, oper: OP) -> None:
        """Reload a single TOX-strategy COMP from its .tox file on disk."""
        rel_tox_path = self.getExternalPath(oper)
        if not rel_tox_path:
            self.Log(f'No TOX file path found for {oper.path}', 'ERROR')
            return

        abs_path = self.buildAbsolutePath(rel_tox_path)
        if not abs_path.is_file():
            self.Log(f'TOX file not found: {rel_tox_path}', 'ERROR')
            return

        # Toggle enableexternaltox to force TD to re-read the .tox
        oper.par.enableexternaltox = False
        oper.par.enableexternaltox = True
        self.Log(f'Reloaded {oper.path} from disk ({rel_tox_path})', 'SUCCESS')

    def HandleEmbed(self, oper: OP) -> None:
        """Toggle per-COMP 'embed DATs' setting and re-export the .tdn."""
        # Read current effective value
        per_comp = oper.fetch('embed_dats_in_tdn', None, search=False)
        if per_comp is not None:
            effective = per_comp
        else:
            effective = self.my.par.Embeddatsintdns.eval()

        # Toggle to explicit opposite
        new_val = not effective
        oper.store('embed_dats_in_tdn', new_val)

        # Re-export the .tdn with the new setting
        rel_tdn_path = self._getStrategyFilePath(oper.path, 'tdn')
        if rel_tdn_path:
            abs_path = str(self.buildAbsolutePath(rel_tdn_path))
            protected = self._getAllTrackedTDNFiles(exclude_path=oper.path)
            self.my.ext.TDN.ExportNetwork(
                root_path=oper.path, output_file=abs_path,
                cleanup_protected=protected)

        state = 'on' if new_val else 'off'
        self.Log(f"Embed DATs set to {state} for {oper.path}", 'SUCCESS')
        self.Refresh()

    def HandlePortableExport(self, oper: OP) -> None:
        """Show a file dialog and export a portable .tox for the given COMP."""
        default_name = f"{oper.name}.tox"
        start_dir = str(Path(project.folder).parents[0])
        path = ui.chooseFile(
            load=False,
            start=start_dir,
            fileTypes=['tox'],
            title='Export portable tox')
        if path is None:
            return
        self.ExportPortableTox(target=oper, save_path=str(path))
        self.Refresh()

    def HandleStrategyRemove(self, oper: OP) -> None:
        """Remove externalization from a COMP or DAT with confirmation dialog."""
        result = ui.messageBox(
            'Remove',
            'Remove this externalization?\n\n'
            'This will delete the external file from disk, clear the\n'
            "operator's externalization tags, and remove the tracking\n"
            'entry. This cannot be undone.\n\n'
            'Operator: ' + oper.path,
            buttons=['Cancel', 'Remove'])

        if result == 1:
            tox_tag = self.my.par.Toxtag.val
            tdn_tag = self.my.par.Tdntag.val

            if tdn_tag in oper.tags:
                self.RemoveTDNEntry(oper.path)
                oper.tags.discard(tdn_tag)
            elif tox_tag in oper.tags:
                rel_fp = self.getExternalPath(oper)
                self.RemoveListerRow(oper.path, rel_fp)
                oper.tags.discard(tox_tag)
                oper.par.externaltox = ''
                oper.par.externaltox.readOnly = False
            elif oper.family == 'DAT':
                active_tag = self._getActiveDATTag(oper)
                if active_tag:
                    rel_fp = self.getExternalPath(oper)
                    self.RemoveListerRow(oper.path, rel_fp)
                    oper.tags.discard(active_tag)
                    oper.par.file = ''
                    oper.par.file.readOnly = False
            elif self._getStrategyFilePath(oper.path, 'tdn'):
                # Table-only TDN entry (e.g., Full Project export) — no tag on operator
                self.RemoveTDNEntry(oper.path)

            self.resetOpColor(oper)
            self.Refresh()

    def HandleDATConvert(self, oper: OP, new_tag: str) -> None:
        """Convert a DAT's externalization to a different format."""
        self.applyTagToOperator(oper, new_tag)
        if new_tag in oper.tags:
            self.ExternalizeImmediate(oper)
        self.Refresh()

    def ExternalizeImmediate(self, oper: OP) -> None:
        """Immediately externalize a single tagged operator.

        If already tracked with the current strategy, re-saves the file.
        If not yet tracked, initializes tracking + saves via handleAddition().
        Avoids the full Update() scan of all dirty operators.
        """
        tox_tag = self.my.par.Toxtag.val
        tdn_tag = self.my.par.Tdntag.val

        is_tox = tox_tag in oper.tags
        is_tdn = tdn_tag in oper.tags
        is_dat = (not is_tox and not is_tdn
                  and oper.family == 'DAT'
                  and any(t in oper.tags for t in self.getTags('DAT')))

        if not is_tox and not is_tdn and not is_dat:
            return

        # Determine strategy for table lookup
        if is_tox:
            strategy = 'tox'
        elif is_tdn:
            strategy = 'tdn'
        else:
            # DAT strategy is the tag value itself (py, json, xml, etc.)
            dat_tags = self.getTags('DAT')
            strategy = next((t for t in dat_tags if t in oper.tags), 'py')

        # Check if already tracked with this strategy
        table = self.Externalizations
        for i in range(1, table.numRows):
            if (table[i, 'path'].val == oper.path
                    and table[i, 'strategy'].val == strategy):
                # Already tracked — just re-save
                if is_tox:
                    self.Save(oper.path)
                elif is_tdn:
                    self.SaveTDN(oper.path)
                # DATs use syncfile — no explicit save needed
                return

        # Not tracked — full initialization (creates tracking entry + saves file)
        self.handleAddition(oper)

    # ==========================================================================
    # PROJECT-WIDE EXTERNALIZATION
    # ==========================================================================

    def ExternalizeProject(self) -> None:
        """Externalize all compatible COMPs and DATs in project."""
        choice = ui.messageBox('Embody',
            'Add all compatible COMPs and DATs to Embody?\n'
            '(Palette components, clones, and replicants will be ignored)',
            buttons=['Cancel', 'TOX', 'TDN'])

        if choice == 0:
            return

        use_tdn = (choice == 2)

        # Find system COMPs to exclude
        sys_comps = self.root.findChildren(
            type=COMP, parName='clone',
            key=lambda x: any(s in (str(x.par.clone.expr) or '') for s in ['TDTox', 'TDBasicWidgets'])
        )

        paths_to_exclude = set()
        for sys_comp in sys_comps:
            paths_to_exclude.add(sys_comp.path)
            for desc in sys_comp.findChildren():
                paths_to_exclude.add(desc.path)

        # Process DATs
        for oper in self.root.findChildren(type=DAT, parName='file'):
            if self._shouldSkipOp(oper, paths_to_exclude):
                continue

            if oper.type in self.supported_dat_types:
                tag_value = self._inferDATTagValue(oper)
                self.applyTagToOperator(oper, tag_value)

        # Process COMPs
        if use_tdn:
            comp_tag = self.my.par.Tdntag.val
            for oper in self.root.findChildren(type=COMP):
                if self._shouldSkipOp(oper, paths_to_exclude):
                    continue
                self.applyTagToOperator(oper, comp_tag)
        else:
            comp_tag = self.my.par.Toxtag.val
            for oper in self.root.findChildren(type=COMP, parName='externaltox'):
                if self._shouldSkipOp(oper, paths_to_exclude):
                    continue
                self.applyTagToOperator(oper, comp_tag)

        self.UpdateHandler()

    def _shouldSkipOp(self, oper, paths_to_exclude):
        """Check if operator should be skipped in project externalization."""
        return (
            oper.path in paths_to_exclude or
            self.isReplicant(oper) or
            self.isInsideClone(oper) or
            oper.path.startswith('/local/') or
            oper.path == '/local'
        )

    # ==========================================================================
    # LISTER ROW REMOVAL
    # ==========================================================================

    def RemoveListerRow(self, op_path: str, rel_file_path: str) -> None:
        """
        Remove an operator from externalization tracking.
        SAFETY: Only deletes the file if it's tracked by Embody and not referenced elsewhere.
        """
        is_clone = False
        
        try:
            oper = op(op_path)
            if oper:
                if 'clone' in oper.tags:
                    is_clone = True
                    self.Log(f"Skipping file deletion for clone: {op_path}", "INFO")
                
                # Remove tags
                for tag in self.getTags():
                    if tag in oper.tags:
                        oper.tags.remove(tag)
                
                # Clear parameters
                if oper.family == 'COMP':
                    oper.par.externaltox = ''
                    oper.par.externaltox.readOnly = False
                elif oper.family == 'DAT':
                    oper.par.syncfile = False
                    oper.par.file = ''
                    oper.par.file.readOnly = False
                
                oper.cook(force=True)
                self.resetOpColor(oper)
                self.param_tracker.removeComp(op_path)
        except Exception as e:
            self.Log(f"Error handling operator '{op_path}'", "ERROR", str(e))

        # Check if file is still referenced by other operators
        normalized_path = self.normalizePath(rel_file_path)
        other_references = self._checkFileReferences(op_path, normalized_path)

        # Delete file only if:
        # 1. It's not a clone reference
        # 2. No other operators reference it
        # 3. It's a file we're tracking (implicit - we got rel_file_path from our table)
        if normalized_path and not other_references and not is_clone:
            full_path = self.buildAbsolutePath(normalized_path).resolve()
            
            def delete_file():
                try:
                    if full_path.is_file():
                        full_path.unlink()

                        # Clean up empty parent directories
                        parent_dir = full_path.parent
                        while parent_dir.exists() and parent_dir != Path(project.folder):
                            try:
                                if not any(parent_dir.iterdir()):
                                    parent_dir.rmdir()
                                    parent_dir = parent_dir.parent
                                else:
                                    break
                            except OSError:
                                break
                    else:
                        self.Log(f"No file found: {normalized_path}", "WARNING")
                except Exception as e:
                    self.Log(f"Error removing file", "ERROR", str(e))
            
            run(delete_file, delayFrames=5)
        elif is_clone or other_references:
            self.Log(f"Preserved file '{normalized_path}' (still in use)", "INFO")

        # Remove from table — match on both path and rel_file_path to avoid
        # deleting sibling rows (e.g. a TDN row when removing the TOX row)
        removed = False
        for i in range(1, self.Externalizations.numRows):
            if (self.Externalizations[i, 'path'].val == op_path
                    and self.normalizePath(self.Externalizations[i, 'rel_file_path'].val) == normalized_path):
                try:
                    self.Externalizations.deleteRow(i)
                    self.Log(f"Removed '{op_path}'", "SUCCESS")
                    removed = True
                except Exception as e:
                    self.Log(f"Error removing from table", "ERROR", str(e))
                break
        if not removed:
            self.Debug(f"No table row for '{op_path}' with file '{normalized_path}' - already removed or never added")

    def _checkFileReferences(self, op_path, normalized_path):
        """Check if any other operators reference a file path."""
        if not normalized_path:
            return False
            
        for comp in self.root.findChildren(type=COMP, parName='externaltox'):
            if comp.path != op_path and self.normalizePath(comp.par.externaltox.eval()) == normalized_path:
                self.Log(f"File still referenced by '{comp.path}'", "INFO")
                return True
        
        for dat in self.root.findChildren(type=DAT, parName='file'):
            if dat.path != op_path and self.normalizePath(dat.par.file.eval()) == normalized_path:
                self.Log(f"File still referenced by '{dat.path}'", "INFO")
                return True
        
        return False

    def RemoveTDNEntry(self, op_path: str) -> None:
        """Remove a TDN strategy entry and delete the .tdn file from disk."""
        self._removeTDNStrategy(op_path)
        self.lister.reset()

    # ==========================================================================
    # TDN RECONSTRUCTION ON START
    # ==========================================================================

    def ReconstructTDNComps(self) -> None:
        """Reconstruct all TDN-strategy COMPs from .tdn files on project open."""
        if not self.my.par.Tdncreateonstart.eval():
            return

        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return

        self.Log(f'Reconstructing {len(tdn_comps)} TDN COMP(s)...', 'INFO')
        errors_total = 0

        for comp_path, rel_tdn_path in tdn_comps:
            comp = op(comp_path)
            if comp is None:
                # COMP was tagged but .toe wasn't saved — create the shell
                comp = self._createMissingCompShell(comp_path, 'tdn')
                if comp is None:
                    errors_total += 1
                    continue

            abs_path = self.buildAbsolutePath(rel_tdn_path)
            if not abs_path.is_file():
                self.Log(f'TDN file not found: {rel_tdn_path}', 'WARNING')
                continue

            try:
                import json
                tdn_doc = json.loads(abs_path.read_text(encoding='utf-8'))
            except Exception as e:
                self.Log(f'Failed to read TDN for {comp_path}: {e}', 'ERROR')
                errors_total += 1
                continue

            # Import from TDN (phases 1-7 + phase 8 file-link restore)
            result = self.my.ext.TDN.ImportNetwork(
                target_path=comp_path,
                tdn=tdn_doc,
                clear_first=True,
                restore_file_links=True,
            )

            if result.get('error'):
                self.Log(f'Reconstruction failed for {comp_path}: {result["error"]}', 'ERROR')
                errors_total += 1
                continue

            created = result.get('created_count', 0)
            restored = result.get('restored_file_links', 0)
            msg = f'Reconstructed {comp_path} ({created} ops'
            if restored:
                msg += f', {restored} file links'
            msg += ')'
            self.Log(msg, 'SUCCESS')

            # Phase E: Post-reconstruction error checking
            comp_errors = self._verifyReconstructedComp(comp)
            if comp_errors:
                errors_total += len(comp_errors)

        # Build report
        self._logReconstructionReport(tdn_comps, errors_total)

    def _getTDNStrategyComps(self) -> list[tuple[str, str]]:
        """Get all TDN-strategy COMPs from the externalizations table.

        Returns list of (comp_path, rel_tdn_path) tuples.
        Never includes Embody itself, its ancestors, or its descendants —
        reconstructing or stripping anything inside Embody would be
        self-destruction.
        """
        table = self.Externalizations
        if not table:
            return []
        if table[0, 'strategy'] is None:
            return []  # Legacy table without strategy column — no TDN entries
        embody_path = self.my.path  # e.g. /embody/Embody — skip regardless of location
        result = []
        for i in range(1, table.numRows):
            if table[i, 'strategy'].val == 'tdn':
                comp_path = table[i, 'path'].val
                # Never include root "/" — stripping it destroys the entire project.
                # Never include Embody, its ancestors, or its descendants.
                if (comp_path == '/'
                        or comp_path == embody_path
                        or embody_path.startswith(comp_path + '/')
                        or comp_path.startswith(embody_path + '/')):
                    continue
                result.append((
                    comp_path,
                    table[i, 'rel_file_path'].val,
                ))
        return result

    def StripCompChildren(self, comp: OP) -> int:
        """Remove children from a TDN-strategy COMP (for smaller .toe).

        Destroys both regular children and utility operators (annotations).
        Returns the number of operators destroyed.
        """
        # findChildren with includeUtility=True gets everything:
        # regular children + hidden utility ops (annotations with utility=True)
        all_ops = list(comp.findChildren(depth=1, includeUtility=True))
        count = len(all_ops)
        n_utility = sum(1 for c in all_ops if getattr(c, 'utility', False))
        for child in all_ops:
            try:
                child.destroy()
            except Exception as e:
                self.Log(f'Failed to destroy {child.path}: {e}', 'WARNING')
        if count:
            self.Log(f'Stripped {count} operators from {comp.path} '
                     f'({count - n_utility} children, {n_utility} annotations)', 'INFO')
        return count

    def _verifyReconstructedComp(self, comp) -> list[str]:
        """Check a reconstructed COMP for TD errors (broken connections, scripts, etc.).

        Returns list of error strings found.
        """
        errors = []
        try:
            for child in comp.findChildren(depth=-1):
                if child.errors:
                    for err in child.errors.split('\n'):
                        err = err.strip()
                        if err:
                            errors.append(f'{child.path}: {err}')
                if child.warnings:
                    for warn in child.warnings.split('\n'):
                        warn = warn.strip()
                        if warn:
                            self.Log(f'Warning in {child.path}: {warn}', 'WARNING')
        except Exception as e:
            self.Log(f'Error checking {comp.path}: {e}', 'WARNING')

        for err in errors:
            self.Log(f'Reconstruction error: {err}', 'ERROR')

        return errors

    def _logReconstructionReport(self, tdn_comps, errors_total) -> None:
        """Log a summary report after TDN reconstruction."""
        count = len(tdn_comps)
        if errors_total:
            self.Log(
                f'TDN reconstruction complete: {count} COMP(s), '
                f'{errors_total} error(s) detected',
                'WARNING')
        else:
            self.Log(
                f'TDN reconstruction complete: {count} COMP(s) rebuilt successfully',
                'SUCCESS')

    def _createMissingCompShell(self, comp_path: str, strategy: str) -> 'OP | None':
        """Create a missing COMP that was tagged but not saved in the .toe.

        Used by both ReconstructTDNComps and RestoreTOXComps when a tracked
        COMP doesn't exist on project open.

        Args:
            comp_path: Full TD path (e.g., '/embody/base_tdn')
            strategy: 'tdn' or 'tox' — determines which tag/color to apply

        Returns:
            The created COMP, or None on failure.
        """
        parent_path = comp_path.rsplit('/', 1)[0] or '/'
        parent_op = op(parent_path)
        if not parent_op or not hasattr(parent_op, 'create'):
            self.Log(f'Cannot create {comp_path}: parent {parent_path} '
                     f'not found or not a COMP', 'WARNING')
            return None

        comp_type = self._getCompTypeFromTable(comp_path) or 'base'
        comp_name = comp_path.rsplit('/', 1)[-1]
        td_type = f'{comp_type}COMP'

        try:
            new_comp = parent_op.create(td_type, comp_name)
        except Exception as e:
            self.Log(f'Failed to create {comp_path} ({td_type}): {e}', 'ERROR')
            return None

        self.Log(f'Created missing COMP shell: {comp_path}', 'INFO')

        # Apply tag and color
        if strategy == 'tdn':
            tag = self.my.par.Tdntag.val
            color = (self.my.par.Tdntagcolorr.eval(),
                     self.my.par.Tdntagcolorg.eval(),
                     self.my.par.Tdntagcolorb.eval())
        else:
            tag = self.my.par.Toxtag.val
            color = (self.my.par.Toxtagcolorr.eval(),
                     self.my.par.Toxtagcolorg.eval(),
                     self.my.par.Toxtagcolorb.eval())
        if tag:
            new_comp.tags.add(tag)
        new_comp.color = color

        # Restore position/color from table metadata
        self._restorePositionFromTable(new_comp, comp_path)

        return new_comp

    def _getCompTypeFromTable(self, comp_path: str) -> str:
        """Read the 'type' column for a COMP from the externalizations table."""
        table = self.Externalizations
        if not table:
            return ''
        for i in range(1, table.numRows):
            if table[i, 'path'].val == comp_path:
                return table[i, 'type'].val
        return ''

    def _restorePositionFromTable(self, comp: 'OP', comp_path: str) -> None:
        """Restore an operator's position and color from the externalizations table."""
        table = self.Externalizations
        if not table:
            return
        # Check if position columns exist
        if table[0, 'node_x'] is None:
            return
        for i in range(1, table.numRows):
            if table[i, 'path'].val == comp_path:
                x_val = table[i, 'node_x'].val
                y_val = table[i, 'node_y'].val
                if x_val and y_val:
                    try:
                        comp.nodeX = int(float(x_val))
                        comp.nodeY = int(float(y_val))
                    except (ValueError, TypeError):
                        pass
                color_val = table[i, 'node_color'].val
                if color_val:
                    try:
                        r, g, b = [float(c) for c in color_val.split(',')]
                        comp.color = (r, g, b)
                    except (ValueError, TypeError):
                        pass
                return

    # ==========================================================================
    # TOX RESTORATION ON START
    # ==========================================================================

    def RestoreTOXComps(self) -> None:
        """Restore missing TOX-strategy COMPs from .tox files on project open.

        For each TOX-strategy entry in the externalizations table where the
        operator is missing but the .tox file exists on disk, creates the COMP
        and sets externaltox to trigger TD's auto-load.
        """
        if not self.my.par.Toxrestoreonstart.eval():
            return

        tox_comps = self._getTOXStrategyComps()
        if not tox_comps:
            return

        # Filter to only missing COMPs with existing .tox files
        to_restore = []
        for comp_path, rel_tox_path, comp_type in tox_comps:
            if op(comp_path):
                continue  # Already exists in .toe — nothing to do
            abs_path = self.buildAbsolutePath(rel_tox_path)
            if not abs_path.is_file():
                self.Log(f'TOX file not found for missing COMP '
                         f'{comp_path}: {rel_tox_path}', 'WARNING')
                continue
            to_restore.append((comp_path, rel_tox_path, comp_type))

        if not to_restore:
            return

        self.Log(f'Restoring {len(to_restore)} TOX COMP(s) from disk...', 'INFO')
        restored = 0
        errors = 0

        for comp_path, rel_tox_path, comp_type in to_restore:
            # Check if it appeared (e.g. loaded as child of a parent .tox)
            if op(comp_path):
                restored += 1
                self.Log(f'COMP {comp_path} already present '
                         f'(loaded from parent .tox)', 'INFO')
                continue

            # Verify parent exists
            parent_path = comp_path.rsplit('/', 1)[0] or '/'
            parent_op = op(parent_path)
            if not parent_op:
                self.Log(f'Parent {parent_path} not found, cannot restore '
                         f'{comp_path}', 'WARNING')
                errors += 1
                continue

            if not hasattr(parent_op, 'create'):
                self.Log(f'Parent {parent_path} is not a COMP, cannot restore '
                         f'{comp_path}', 'WARNING')
                errors += 1
                continue

            comp_name = comp_path.rsplit('/', 1)[-1]
            td_type = f'{comp_type}COMP'

            try:
                new_comp = parent_op.create(td_type, comp_name)
            except Exception as e:
                self.Log(f'Failed to create {comp_path} '
                         f'(type {td_type}): {e}', 'ERROR')
                errors += 1
                continue

            # Set externaltox to trigger TD auto-load from .tox
            try:
                new_comp.par.externaltox = self.normalizePath(rel_tox_path)
                new_comp.par.externaltox.readOnly = True
                new_comp.par.enableexternaltox = True

                # Handle timing issue (same workaround as
                # _setupCompForExternalization)
                if ("Cannot load external tox from path"
                        in new_comp.scriptErrors()):
                    new_comp.allowCooking = False
                    run(lambda p=new_comp.path: self._safeAllowCooking(p, True),
                        delayFrames=1)

                # Re-apply Embody tag and color (may not survive .tox load)
                tox_tag = self.my.par.Toxtag.val
                if tox_tag and tox_tag not in new_comp.tags:
                    new_comp.tags.add(tox_tag)
                new_comp.color = (self.my.par.Toxtagcolorr.eval(),
                                  self.my.par.Toxtagcolorg.eval(),
                                  self.my.par.Toxtagcolorb.eval())

                # Restore position from table metadata
                self._restorePositionFromTable(new_comp, comp_path)

                restored += 1
                self.Log(f'Restored {comp_path} from {rel_tox_path}', 'SUCCESS')

            except Exception as e:
                self.Log(f'Failed to configure externaltox for '
                         f'{comp_path}: {e}', 'ERROR')
                errors += 1

        self._logTOXRestorationReport(len(to_restore), restored, errors)

    def _getTOXStrategyComps(self) -> list[tuple[str, str, str]]:
        """Get all TOX-strategy COMPs from the externalizations table.

        Returns list of (comp_path, rel_tox_path, comp_type) tuples,
        sorted by path depth (shallowest first) so parents are created
        before children.

        Never includes Embody itself, its ancestors, or its descendants.
        """
        table = self.Externalizations
        if not table:
            return []
        if table[0, 'strategy'] is None:
            return []  # Legacy table without strategy column
        embody_path = self.my.path
        result = []
        for i in range(1, table.numRows):
            if table[i, 'strategy'].val == 'tox':
                comp_path = table[i, 'path'].val
                # Never include Embody, its ancestors, or its descendants
                if (comp_path == '/'
                        or comp_path == embody_path
                        or embody_path.startswith(comp_path + '/')
                        or comp_path.startswith(embody_path + '/')):
                    continue
                result.append((
                    comp_path,
                    table[i, 'rel_file_path'].val,
                    table[i, 'type'].val,
                ))
        # Sort by path depth — parents first
        result.sort(key=lambda x: x[0].count('/'))
        return result

    def _logTOXRestorationReport(self, total, restored, errors) -> None:
        """Log a summary report after TOX restoration."""
        if errors:
            self.Log(
                f'TOX restoration complete: {restored}/{total} COMP(s) '
                f'restored, {errors} error(s)',
                'WARNING')
        else:
            self.Log(
                f'TOX restoration complete: {restored} COMP(s) restored '
                f'successfully',
                'SUCCESS')

    # ==========================================================================
    # FILE UTILITIES
    # ==========================================================================

    def deleteFile(self, oper: OP, externalizationsFolder: str) -> None:
        """
        Delete externalized file for an operator.
        SAFETY: This only deletes files at paths we generate for tracked operators.
        """
        abs_folder_path, save_file_path, _, _ = self.getOpPaths(oper, externalizationsFolder)
        if save_file_path is None:
            return

        save_file = save_file_path.resolve()
        try:
            if save_file.exists():
                save_file.unlink()
                self.Log(f"Deleted file: {save_file}", "INFO")
                try:
                    # Only remove directory if empty
                    abs_folder_path.rmdir()
                except OSError:
                    pass  # Directory not empty - this is fine
        except FileNotFoundError:
            self.Log(f"File not found: {save_file}", "WARNING")
        except PermissionError as e:
            self.Log(f"Permission denied deleting file {save_file}: {e}", "WARNING")
            pass
        except Exception as e:
            self.Log(f"Unexpected error deleting file {save_file}: {e}", "WARNING")
            pass

    def deleteEmptyDirectories(self, path: Union[str, Path]) -> None:
        """
        Recursively delete empty directories only.
        SAFETY: rmdir() only succeeds on empty directories.
        """
        empty_dir_found = True
        iteration = 0
        
        while empty_dir_found and iteration < 10:
            empty_dir_found = False
            iteration += 1
            
            for root, dirs, files in os.walk(path, topdown=False):
                for dir_name in dirs:
                    dir_path = str(Path(root) / dir_name)
                    if not list(Path(dir_path).iterdir()):
                        try:
                            Path(dir_path).rmdir()
                            self.Log(f"Deleted empty directory: {dir_path}", "INFO")
                            empty_dir_found = True
                        except OSError as e:
                            self.Log(f"Error deleting directory: {dir_path}", "ERROR", str(e))

    # ==========================================================================
    # UI HELPERS
    # ==========================================================================

    def DirtyCount(self) -> int:
        """Return the number of dirty externalized operators.

        Checks live oper.dirty for COMPs (TD's native dirty flag updates
        immediately when a COMP is modified, but the Externalizations table
        is only refreshed during Refresh/Update). Falls back to the cached
        table value for DATs and 'Par' (parameter change) state.
        """
        table = self.Externalizations
        if not table:
            return 0
        count = 0
        for i in range(1, table.numRows):
            op_path = str(table[i, 'path'].val)
            oper = op(op_path)
            if oper and oper.valid and oper.family == 'COMP':
                if oper.dirty:
                    count += 1
                    continue
                # Check table for 'Par' state (parameter changes detected
                # during Refresh, not reflected in oper.dirty)
                val = str(table[i, 'dirty'].val)
                if val == 'Par':
                    count += 1
                continue
            # For DATs or missing operators, use cached table value
            val = str(table[i, 'dirty'].val)
            if val and val not in ('', 'False', 'Clean', 'Saved'):
                count += 1
        return count

    def Manager(self, action: str) -> None:
        """Open or close the manager window."""
        win = self.my.op('window_manager')
        if action == 'open':
            win.par.winopen.pulse()
            self.Refresh()
        elif action == 'close':
            win.par.winclose.pulse()

    def resetOpColor(self, oper: OP) -> None:
        """Reset operator to default color."""
        oper.color = (0.55, 0.55, 0.55)

    def getProjectFolder(self) -> str:
        """Get project folder path."""
        if self.my.par.Folder.mode == ParMode.EXPRESSION:
            return self.my.par.Folder.eval()
        return str(Path(project.folder) / self.my.par.Folder)

    def getSaveFolder(self) -> str:
        """Get save folder path."""
        if self.my.par.Folder.expr:
            return self.my.par.Folder.eval()
        return project.folder + '/' + self.my.par.Folder

    def OpenSaveFolder(self) -> None:
        """Open externalization folder in file browser."""
        save_folder = str(Path(self.getSaveFolder()).resolve())

        try:
            if sys.platform.startswith('darwin'):
                result = subprocess.call(['open', save_folder])
                if result != 0:
                    self.Log(f'Failed to open folder: {save_folder}', 'WARNING')
            elif sys.platform.startswith('win'):
                os.startfile(save_folder)
        except Exception as e:
            self.Log(f'Failed to open folder: {e}', 'ERROR')

    def OpenSaveFile(self, rel_file_path: str) -> None:
        """Open file location in file browser."""
        filepath = str(self.buildAbsolutePath(self.normalizePath(rel_file_path)).resolve())

        try:
            if sys.platform.startswith('darwin'):
                result = subprocess.call(['open', '-R', filepath])
                if result != 0:
                    self.Log(f'Failed to open file location: {filepath}', 'WARNING')
            elif sys.platform.startswith('win'):
                filepath = filepath.replace('/', '\\')
                result = subprocess.call(['explorer', '/select,', filepath])
                if result != 0:
                    self.Log(f'Failed to open file location: {filepath}', 'WARNING')
        except Exception as e:
            self.Log(f'Failed to open file location: {e}', 'ERROR')

    def OpenTable(self) -> None:
        """Open externalizations table viewer."""
        self.Externalizations.openViewer()

    def MissingExternalizationsPar(self) -> None:
        """Log error for missing externalizations table."""
        self.Log("Missing Externalization tableDAT - required for operation", "ERROR")

    def ImportTDNFromDialog(self) -> None:
        """Open file dialog and import selected .tdn file.

        Auto-detects the target COMP from the file's location relative to
        project.folder using Embody's bijective naming convention. If the
        target exists and has children, prompts Replace/Keep Both/Cancel.
        Falls back to Current Network/Project Root dialog when the target
        cannot be inferred.
        """
        path = ui.chooseFile(fileTypes=['tdn'], title='Import TDN File')
        if not path:
            return

        clear_first = False
        network_path = self._inferTargetFromPath(str(path))

        if network_path:
            target_comp = op(network_path)
            if target_comp and hasattr(target_comp, 'create'):
                child_count = len(target_comp.children)
                if child_count > 0:
                    choice = ui.messageBox('Import TDN',
                        f'Target: {network_path}\n'
                        f'Contains {child_count} operator{"s" if child_count != 1 else ""}.\n\n'
                        f'Existing contents will be replaced.',
                        buttons=['Replace', 'Keep Both', 'Cancel'])
                    if choice == 0:
                        clear_first = True
                    elif choice == 1:
                        clear_first = False
                    else:
                        return
                # else: empty target, import silently
            else:
                network_path = None  # COMP doesn't exist, fall through

        if not network_path:
            choice = ui.messageBox('Import TDN',
                f'Import into which network?\n\nFile: {path}',
                buttons=['Current Network', 'Project Root', 'Cancel'])
            if choice == 0:
                pane = ui.panes.current
                network_path = pane.owner.path if pane and pane.owner else '/'
            elif choice == 1:
                network_path = '/'
            else:
                return

        self._import_clear_first = clear_first
        self.my.par.Tdnfile = str(path)
        self.my.par.Networkpath = network_path
        self.my.par.Importtdn.pulse()

    def _inferTargetFromPath(self, file_path: str) -> Optional[str]:
        """Derive a TD COMP path from a .tdn file's location relative to project.folder.

        Uses Embody's bijective naming convention:
            {project.folder}/embody/base1.tdn → /embody/base1

        Returns the TD path string, or None if the file is outside the project.
        """
        try:
            rel = Path(file_path).relative_to(project.folder)
        except ValueError:
            return None  # File is outside project folder
        stem = str(rel).replace('\\', '/').removesuffix('.tdn')
        if not stem:
            return None
        # Check if this is a project-root export (filename matches project name)
        project_name = project.name.removesuffix('.toe')
        if stem == project_name:
            return '/'
        return '/' + stem

    # ==========================================================================
    # LOGGING
    # ==========================================================================

    def Log(self, message: str, level: str = 'INFO', details: Optional[str] = None, _depth: int = 1) -> None:
        """
        Centralized logging with auto caller detection, FIFO DAT storage,
        ring buffer for MCP access, and optional file logging.

        Accessible globally as op.Embody.Log(message, level).

        Args:
            message: Main message
            level: 'INFO', 'WARNING', 'ERROR', 'SUCCESS', or 'DEBUG'
            details: Optional additional details
            _depth: Stack frame depth for caller detection (internal use)
        """
        # Auto-detect caller via inspect
        frame = inspect.currentframe()
        for _ in range(_depth):
            frame = frame.f_back
        caller_locals = frame.f_locals
        caller_info = None

        if 'self' in caller_locals and hasattr(caller_locals['self'], '__class__'):
            ext = caller_locals['self']
            caller_info = f"{ext.__class__.__name__}"
        elif 'me' in caller_locals:
            caller_info = f"{caller_locals['me'].path}"
        else:
            frame_info = inspect.getframeinfo(frame)
            caller_info = f"{os.path.basename(frame_info.filename)}:{frame_info.lineno}"

        time_str = datetime.now().strftime("%H:%M:%S")
        current_frame = absTime.frame

        # Append structured entry to ring buffer for MCP access (all levels)
        self._log_counter += 1
        self._log_buffer.append({
            'id': self._log_counter,
            'timestamp': datetime.now().isoformat(),
            'frame': current_frame,
            'level': level,
            'source': caller_info,
            'message': message,
            'details': details,
        })

        # Skip DEBUG output to FIFO/textport/file unless Verbose is enabled
        if level == 'DEBUG' and not self.my.par.Verbose:
            return

        # Structured log entry string
        log_entry = f"{time_str} {current_frame:>7} {level:<7} {caller_info}: {message}"
        if details:
            log_entry += f"\n    Details: {details}"

        # Output to FIFO DAT
        if self._fifo:
            self._fifo.appendRow([log_entry])

        # Print to textport if enabled
        if self.my.par.Print:
            print(log_entry)

        # File logging if enabled
        if self.my.par.Logtofile and self.my.par.Logfolder:
            try:
                self._write_log_to_file(log_entry)
            except Exception as e:
                print(f"Error writing to log file: {e}")

    def Debug(self, msg: str) -> None:
        """Log a DEBUG level message."""
        self.Log(msg, level='DEBUG', _depth=2)

    def Info(self, msg: str) -> None:
        """Log an INFO level message."""
        self.Log(msg, level='INFO', _depth=2)

    def Warn(self, msg: str) -> None:
        """Log a WARNING level message."""
        self.Log(msg, level='WARNING', _depth=2)

    def Error(self, msg: str) -> None:
        """Log an ERROR level message."""
        self.Log(msg, level='ERROR', _depth=2)

    # --- File Logging Helpers ---

    LOG_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    def _get_log_file_path(self):
        """
        Build the current log file path.
        Format: <Logfolder>/<project.name>_YYMMDD.log
        Rotates to _001, _002, etc. when file exceeds LOG_MAX_FILE_SIZE.
        """
        log_folder = self.my.par.Logfolder.eval()
        if not log_folder:
            return None

        # Ensure folder exists (relative path OK)
        os.makedirs(log_folder, exist_ok=True)

        date_str = datetime.now().strftime('%y%m%d')
        proj_name = project.name
        base_name = f'{proj_name}_{date_str}'

        # Check base file first
        base_path = os.path.join(log_folder, f'{base_name}.log')
        if not os.path.exists(base_path) or os.path.getsize(base_path) < self.LOG_MAX_FILE_SIZE:
            return base_path

        # Find next rotation index
        idx = 1
        while True:
            rotated_path = os.path.join(log_folder, f'{base_name}_{idx:03d}.log')
            if not os.path.exists(rotated_path) or os.path.getsize(rotated_path) < self.LOG_MAX_FILE_SIZE:
                return rotated_path
            idx += 1

    def _write_log_to_file(self, log_entry):
        """Write a log entry to the current log file."""
        file_path = self._get_log_file_path()
        if file_path:
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write(log_entry + '\n')


# ==============================================================================
# PARAMETER TRACKER
# ==============================================================================

class ParameterTracker:
    """Tracks parameter changes on COMPs to detect dirty state."""

    def __init__(self, ownerComp):
        self.my = ownerComp
        self.param_store = {}
        
    def captureParameters(self, comp):
        """Capture all parameters of a COMP."""
        params = {}
        for page in comp.pages + comp.customPages:
            for par in page.pars:
                if par.name in ['externaltox', 'file']:
                    continue
                params[par.name] = {
                    'value': par.eval(),
                    'expr': par.expr if par.expr else None,
                    'bindExpr': par.bindExpr if par.bindExpr else None,
                    'mode': par.mode
                }
        return params
    
    def updateParamStore(self, comp):
        """Update stored parameters for a COMP."""
        self.param_store[comp.path] = self.captureParameters(comp)
        
    def compareParameters(self, comp):
        """Compare current parameters with stored. Returns True if changed."""
        if comp.path not in self.param_store:
            self.updateParamStore(comp)
            return False
            
        stored = self.param_store[comp.path]
        current = self.captureParameters(comp)
        
        # Check for additions/removals
        if set(current.keys()) != set(stored.keys()):
            return True
        
        # Check values
        for name in stored:
            if name not in current:
                return True
            if (stored[name]['value'] != current[name]['value'] or
                stored[name]['expr'] != current[name]['expr'] or
                stored[name].get('bindExpr') != current[name].get('bindExpr') or
                stored[name]['mode'] != current[name]['mode']):
                return True
        
        return False
    
    def removeComp(self, comp_path):
        """Remove a COMP from tracking."""
        self.param_store.pop(comp_path, None)

    def initializeTracking(self, embody):
        """Initialize tracking for all externalized COMPs."""
        self.param_store = {}
        for comp in embody.getExternalizedOps(COMP):
            self.updateParamStore(comp)
            embody.Log(f"Initialized tracking for {comp.path}", "INFO")