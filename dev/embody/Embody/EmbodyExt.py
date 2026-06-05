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

    # Rule DAT name -> slug (shared across all AI clients)
    _TEMPLATE_MAP_RULES = {
        'text_rule_network_layout':          'network-layout',
        'text_rule_td_python':               'td-python',
        'text_rule_mcp_safety':              'mcp-safety',
        'text_rule_parameters':              'parameters',
    }

    # Skill DAT name -> slug (Claude Code only)
    _TEMPLATE_MAP_SKILLS = {
        'text_skill_create_operator':     'create-operator',
        'text_skill_debug_operator':      'debug-operator',
        'text_skill_externalize':         'externalize-operator',
        'text_skill_create_extension':    'create-extension',
        'text_skill_manage_annotations':  'manage-annotations',
        'text_skill_td_api_reference':    'td-api-reference',
        'text_skill_mcp_tools_reference': 'mcp-tools-reference',
    }

    # Parameters persisted to .embody/config.json across upgrades.
    # Explicit whitelist -- new params default to "not persisted" until added.
    _PERSISTED_PARAMS = frozenset({
        # Core
        'Folder', 'Envoyenable', 'Envoyport', 'Aiclient', 'Aiprojectroot',
        'Aiprojectrootcustom',
        # Tag names
        'Toxtag', 'Tdntag', 'Tdnexcludetag', 'Pytag', 'Csvtag', 'Dattag',
        'Htmltag', 'Jsontag', 'Mdtag', 'Rtftag', 'Txttag',
        'Xmltag', 'Glsltag', 'Tsvtag',
        # Tag colors
        'Toxtagcolorr', 'Toxtagcolorg', 'Toxtagcolorb',
        'Tdntagcolorr', 'Tdntagcolorg', 'Tdntagcolorb',
        'Clonetagcolorr', 'Clonetagcolorg', 'Clonetagcolorb',
        'Taggingmenucolorr', 'Taggingmenucolorg', 'Taggingmenucolorb',
        'Dattagcolorr', 'Dattagcolorg', 'Dattagcolorb',
        # Behavior
        'Logfolder', 'Logtofile', 'Verbose', 'Print',
        'Detectduplicatepaths', 'Templatemaster', 'Localtimestamps',
        # TDN
        'Tdnmode',
        'Embeddatsintdns', 'Embedstorageintdns', 'Tdndatsafety',
        'Tdncascade', 'Tdncreateonstart', 'Tdnstriponsave',
        'Toxrestoreonstart', 'Datrestoreonstart', 'Filecleanup',
    })

    # Duplicate-path prompt: above this many operators in one group, a
    # button-per-operator row becomes unreadable (and overflows the dialog),
    # so we switch to a strategy prompt instead. See _promptForDuplicateGroup.
    _MAX_MANUAL_BUTTONS = 5

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

        # Network fingerprints for TDN COMPs -- used instead of oper.dirty
        # (which is always True when externaltox is empty)
        self._tdn_fingerprints = {}

        # NOTE: _setupEnvironment() is NOT called here.
        # It runs inside EnvoyExt.Start(), which is invoked after init() and
        # _restoreSettings() have run. Calling it here (based on the baked
        # Envoyenable value) would bypass the opt-in prompt on fresh .tox drop.

    # ==========================================================================
    # PYTHON ENVIRONMENT SETUP (uv)
    # ==========================================================================

    # Bump MCP_MIN_VERSION when a new release is tested and verified.
    MCP_MIN_VERSION = '1.26.0'

    def _venvPaths(self) -> dict:
        """Compute venv / site-packages paths and the dependency list.

        Reads ``project.folder`` (a TouchDesigner global), so this MUST run on
        the main thread. The returned dict is plain data -- safe to hand to a
        worker thread (see _installDependencies), which is the whole point of
        separating it from the install work.
        """
        project_dir = project.folder
        venv_dir = os.path.join(project_dir, '.venv')
        python_exe = sys.executable  # current interpreter (cross-platform)
        deps = [f'mcp>={self.MCP_MIN_VERSION}', 'attrs<25']
        if sys.platform.startswith('win'):
            site_packages = os.path.join(venv_dir, 'Lib', 'site-packages')
            venv_python = os.path.join(venv_dir, 'Scripts', 'python.exe')
            deps.append('pywin32>=306')
        else:
            py_ver = f'python{sys.version_info.major}.{sys.version_info.minor}'
            site_packages = os.path.join(venv_dir, 'lib', py_ver, 'site-packages')
            venv_python = os.path.join(venv_dir, 'bin', 'python')
        return {
            'project_dir': project_dir,
            'venv_dir': venv_dir,
            'site_packages': site_packages,
            'venv_python': venv_python,
            'python_exe': python_exe,
            'deps': deps,
            'mcp_min_version': self.MCP_MIN_VERSION,
        }

    def _environmentNeedsInstall(self, spec: Optional[dict] = None) -> bool:
        """Cheap, non-blocking check: does the venv need a (slow) install?

        Returns True when a venv build / pip install is required -- because the
        mcp package is absent, below MCP_MIN_VERSION, or paired with an
        incompatible attrs 25.x. Reads only the filesystem (the installed
        version is parsed from the ``mcp-X.Y.Z.dist-info`` directory name), so
        there is no subprocess, no network, and no import. Safe to call on the
        main thread before every Start() to decide sync-vs-async bootstrap.
        """
        spec = spec or self._venvPaths()
        site_packages = spec['site_packages']
        if not os.path.isdir(os.path.join(site_packages, 'mcp')):
            return True
        try:
            infos = glob(os.path.join(site_packages, 'mcp-*.dist-info'))
            if not infos:
                # mcp present but no metadata -- the old fast path accepted this
                # and proceeded to the import check, so no install is required.
                return False
            ver = os.path.basename(infos[0])[len('mcp-'):-len('.dist-info')]
            installed = tuple(int(x) for x in ver.split('.')[:3])
            minimum = tuple(int(x) for x in spec['mcp_min_version'].split('.'))
            if installed < minimum:
                return True
        except Exception:
            return False
        # attrs 25.x conflicts with TD's bundled attr module -- needs a downgrade.
        try:
            attrs_infos = glob(os.path.join(site_packages, 'attrs-*.dist-info'))
            if attrs_infos:
                aver = os.path.basename(attrs_infos[0])[len('attrs-'):-len('.dist-info')]
                if int(aver.split('.')[0]) >= 25:
                    return True
        except Exception:
            pass
        return False

    def _setupEnvironment(self):
        """
        Set up a Python virtual environment using uv for Envoy dependencies.
        Installs uv if not found, creates .venv, installs packages.
        Adds the venv's site-packages to sys.path so TD can import from it.

        Returns True if the environment is ready (mcp.server importable),
        False if any step failed. Callers (e.g. EnvoyExt.Start) MUST gate on
        this -- continuing past a False return produces an inscrutable
        'No module named mcp.server' traceback at server-start time.

        Synchronous. The slow install runs on the calling thread, so this is
        only safe to call inline when _environmentNeedsInstall() is False (the
        fast path) or when blocking is acceptable (the venv-recreate recovery
        path in EnvoyExt._configureMCPClient). EnvoyExt.Start() routes the
        install-needed case through a background thread instead -- see
        _installDependencies and EnvoyExt._beginAsyncBootstrap.
        """
        spec = self._venvPaths()
        site_packages = spec['site_packages']

        if self._environmentNeedsInstall(spec):
            msgs = []
            ok = self._installDependencies(
                spec, log=lambda m, lvl='INFO': msgs.append((lvl, m)))
            for lvl, m in msgs:
                self.Log(m, lvl)
            if not ok:
                return False

        self._addSitePackages(site_packages)
        if sys.platform.startswith('win'):
            self._fixPywin32Dlls(site_packages)

        # Opportunistic, non-blocking check for a newer mcp on PyPI.
        try:
            from importlib.metadata import version as pkg_version
            self._checkMCPUpdate(pkg_version('mcp'))
        except Exception:
            pass

        return self._verifyMcpImportable(site_packages)

    def _installDependencies(self, spec: dict, log) -> bool:
        """Build the venv and pip-install Envoy's dependencies.

        WORKER-THREAD SAFE: touches no TouchDesigner objects. Every path is
        precomputed in ``spec`` (see _venvPaths, which must run on the main
        thread), and every message goes through the ``log(message, level)``
        callback -- never self.Log, which writes the FIFO DAT and reads
        parameters (illegal off the main thread). Returns True if uv, the venv,
        and the dependencies all installed cleanly.

        Does NOT touch sys.path or import mcp -- callers do that on the main
        thread afterward (see _setupEnvironment / EnvoyExt._pollBootstrap), so
        the delicate pydantic_core import stays where it belongs.
        """
        venv_dir = spec['venv_dir']
        venv_python = spec['venv_python']
        python_exe = spec['python_exe']
        deps = spec['deps']
        try:
            uv = self._findOrInstallUv(python_exe, log=log)
            if not uv:
                log('uv not found and could not be installed -- Envoy cannot '
                    'bootstrap. Install uv manually (https://docs.astral.sh/uv/) '
                    'and ensure it is on PATH visible to TouchDesigner (macOS GUI '
                    'apps do not inherit shell PATH).', 'ERROR')
                return False

            # Create venv if it doesn't exist.
            # stdin=DEVNULL: subprocess.run from inside TD on Windows raises
            # [WinError 50] without it -- subprocess.py's stdin=None path
            # calls DuplicateHandle on TD's stdin handle, which is not
            # duplicatable for a GUI process. DEVNULL routes through NUL.
            if not os.path.isdir(venv_dir):
                log('Creating virtual environment...')
                subprocess.run(
                    [uv, 'venv', venv_dir, '--python', python_exe],
                    check=True, capture_output=True, text=True,
                    stdin=subprocess.DEVNULL,
                )

            log('Installing dependencies...')
            subprocess.run(
                [uv, 'pip', 'install'] + deps + ['--python', venv_python],
                check=True, capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
            )
            log('Python environment ready', 'SUCCESS')
            return True

        except subprocess.CalledProcessError as e:
            log(f'Environment setup failed: {e.stderr or e}', 'ERROR')
            return False
        except Exception as e:
            log(f'Environment setup failed: {e}', 'ERROR')
            return False

    def _verifyMcpImportable(self, site_packages):
        """Final gate: confirm mcp.server actually imports inside TD's process.

        A populated site-packages is necessary but not sufficient -- a partial
        install or load-time failure (missing native dep, etc.) would still
        leave the server unable to start. Catching it here yields a useful
        textport message instead of an inscrutable traceback at run time.

        Fast path: if mcp.server is already in sys.modules, a previous Start()
        in this session already imported it successfully -- return True without
        touching sys.modules.  Tearing down and re-importing mcp.* on top of an
        already-loaded pydantic_core (Rust C extension) can panic the
        validator and abort() the process with no Python traceback -- the
        "TD just closes on Envoy toggle off/on" crash users hit on 5.0.393+.
        """
        if 'mcp.server' in sys.modules:
            return True
        try:
            import importlib
            # First import attempt of this session, or recovery from a prior
            # failed import: clear any half-loaded mcp.* entries so the loader
            # genuinely re-runs (a failed import leaves the parent package
            # behind but not the submodule).
            for mod in list(sys.modules):
                if mod == 'mcp' or mod.startswith('mcp.'):
                    del sys.modules[mod]
            importlib.import_module('mcp.server')
            return True
        except Exception as e:
            self.Log(
                f'Dependencies installed but mcp.server failed to import: {e}. '
                f'Inspect {site_packages} for partial installs and try deleting '
                f'.venv/ to force a clean rebuild.',
                'ERROR',
            )
            return False

    def _findOrInstallUv(self, python_exe, log=None):
        """Find uv on PATH, or install it via pip --user. Returns path to uv executable or None.

        ``log`` is a ``log(message, level='INFO')`` callback. It defaults to
        self.Log for main-thread callers; worker-thread callers
        (_installDependencies) MUST pass a thread-safe collector instead, since
        self.Log writes the FIFO DAT and reads parameters.
        """
        log = log or self.Log
        # Check PATH first
        uv = shutil.which('uv')
        if uv:
            return uv

        # Install uv via pip --user (avoids needing admin for Program Files)
        log('uv not found - installing via pip...')
        try:
            subprocess.run(
                [python_exe, '-m', 'pip', 'install', '--user', 'uv'],
                check=True, capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as e:
            log(f'Failed to install uv: {e.stderr or e}', 'ERROR')
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

        log('Could not find uv after install - is Python user Scripts on PATH?', 'ERROR')
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

        owner_path = self.my.path

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
                    msg = (
                        f'MCP update available: {installed} -> {latest}. '
                        f'Update EmbodyExt.MCP_MIN_VERSION '
                        f'and delete dev/.venv to upgrade.'
                    )
                    # self.Log() touches TD objects (FIFO DAT, parameters,
                    # absTime.frame). Marshal to the main thread via run().
                    # Guarded so a rename/move between spawn and fire becomes
                    # a silent no-op rather than a None.Log() script error.
                    run("o = op(args[0])\nif o: o.Log(args[1], 'WARNING')",
                        owner_path, msg, delayFrames=1)
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

    @property
    def TDNBackupDir(self) -> Path:
        """Returns the .tdn_backup directory path (under the project root)."""
        return Path(project.folder) / '.tdn_backup'

    def _cellVal(self, row, col, default: str = '') -> str:
        """Safe read of an externalizations table cell.

        TD's `table[row, col]` returns None when the column doesn't exist or
        the row-key lookup misses, and `None.val` then raises AttributeError.
        Issue #21 traced multiple crashes (`'NoneType' object has no
        attribute 'val'`) to such reads after a partial ExternalizeProject
        cascade left the table in an inconsistent state.

        Returns the cell's string value, or `default` (empty string) when
        the table or the cell is missing.

        A missing cell is only WARNED about when it is a genuine row-level
        inconsistency: an INTEGER row that exists (0 < row < numRows) whose
        declared column has no cell -- i.e. a short/partially-written row from
        a half-completed cascade (issue #21). The quiet cases are expected and
        common: a string path-key that simply isn't tracked (a normal "is this
        op in the table?" lookup), and a column absent from the header (legacy
        pre-strategy table).
        """
        table = self.Externalizations
        if table is None:
            return default
        cell = table[row, col]
        if cell is None:
            if (isinstance(row, int) and 0 < row < table.numRows
                    and table[0, col] is not None):
                self.Log(
                    f"Externalizations row {row} missing cell for column "
                    f"{col!r} -- table may be inconsistent (treating as "
                    f"empty)", "WARNING")
            return default
        return cell.val

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

    def _messageBox(self, title, message, buttons):
        """ui.messageBox with auto-response support for headless testing.

        Seed responses via:
            op.Embody.store('_smoke_test_responses', {'Dialog Title': button_index})

        A list value answers multiple invocations of the same title in
        order (one button_index per invocation):
            op.Embody.store('_smoke_test_responses', {'Dialog Title': [1, 2]})

        Single-int values are consumed on first use; list values are
        consumed front-to-back until empty. The key is removed once
        its responses are exhausted; the store is cleared when no
        keys remain.
        """
        responses = self.my.fetch('_smoke_test_responses', None, search=False)
        test_mode = responses is not None
        if test_mode and title in responses:
            value = responses[title]
            if isinstance(value, list):
                choice = value.pop(0) if value else None
                if choice is None:
                    # List exhausted -- treat as a hard test failure (do NOT
                    # fall back to ui.messageBox, which would freeze TD with
                    # modal dialogs queued by the test).
                    self.Log(
                        f'[test] Response list exhausted for "{title}"; '
                        f'returning -1 instead of opening modal dialog. '
                        f'Seed a longer list if more invocations are expected.',
                        'WARNING')
                    return -1
                if not value:
                    responses.pop(title)
            else:
                choice = responses.pop(title)
            self.Log(f'[test] Auto-responded to "{title}" -> button {choice}')
            if not responses:
                self.my.unstore('_smoke_test_responses')
            return choice
        if test_mode:
            # Test is running but no response was seeded for this title --
            # bail with -1 instead of opening a modal that would freeze TD.
            self.Log(
                f'[test] No response seeded for "{title}"; returning -1 '
                f'instead of opening modal dialog. Seed it via '
                f'op.Embody.store("_smoke_test_responses", {{...}}).',
                'WARNING')
            return -1
        return ui.messageBox(title, message, buttons=buttons)

    def _promptEnvoy(self):
        """Prompt user to enable Envoy (AI coding assistant integration)."""
        choice = self._messageBox('Embody - AI Coding Assistant Integration',
            'Enable Envoy?\n\n'
            'Envoy is an MCP server that lets AI coding assistants\n'
            'create, modify, and query TouchDesigner operators.\n\n'
            'This will:\n'
            '  - Install Python dependencies (~30 MB)\n'
            '  - Start a local MCP server on port '
            f'{self.my.par.Envoyport.eval()}\n'
            '  - Generate AI config files in your project root\n'
            '    (CLAUDE.md, AGENTS.md, .mcp.json, .claude/ rules + skills)\n\n'
            'All Envoy MCP tools are auto-authorized for convenience.\n'
            'To adjust permissions, edit .claude/settings.local.json\n'
            'in your project root after setup.\n\n'
            'Works with Claude Code, Cursor, Windsurf, and other MCP clients.\n'
            'You can change this later via the Envoyenable parameter.\n\n'
            'Note: TD will be unresponsive for a few seconds while\n'
            'dependencies are installed.',
            buttons=['Skip', 'Enable Envoy'])

        if choice == 1:
            self._enableEnvoy()
        else:
            self.my.par.Envoyenable = False
            self.Log('Envoy skipped. Enable later via Envoyenable parameter.', 'INFO')

    def _enableEnvoy(self):
        """Enable Envoy: git check, install deps, extract AI config, start server."""
        self.Log('Setting up Envoy...', 'INFO')

        # Git check runs FIRST -- immediately after the user clicks "Enable Envoy",
        # before the slow deps install. This keeps all dialogs at the start of the
        # setup flow so nothing surprising appears after TD goes unresponsive.
        git_root = self.my.ext.Envoy._checkOrInitGitRepo()
        if git_root is None:
            # User cancelled -- abort Envoy setup entirely.
            self.Log('Envoy setup cancelled.', 'INFO')
            return
        # Store so Start() skips re-prompting for git.
        self.my.store('_git_root', str(git_root))

        # Install Python dependencies
        self._setupEnvironment()

        # Extract AI coding assistant config files to project/repo root
        self._extractAIConfig()

        # Enable Envoy (triggers Start() via parexec.py)
        self.my.par.Envoyenable = True
        self.my.par.Envoystatus = 'Starting...'

        client_label = self.my.par.Aiclient.label
        self.Log(
            f'Envoy enabled! Config generated for {client_label}. '
            f'Connect your AI coding assistant via MCP.',
            'SUCCESS'
        )

    def _findProjectRoot(self):
        """Where Embody writes AI config, MCP config, and its own state.

        Honors the Aiprojectroot parameter:
          - 'gitroot' (default): the git repository root, found by walking
            up from project.folder. This is where AI tools (Claude Code,
            Cursor, etc.) expect AGENTS.md / .mcp.json / .claude/ to live
            when the whole repo is the workspace.
          - 'projectfolder': the directory containing the .toe. Use this
            when the TD project lives in a subdirectory of a larger repo
            and you open that subdirectory as your AI tool's workspace.
        """
        # getattr-based access: lets older .toes without Aiprojectroot keep
        # working with the legacy git-root behavior.
        mode_par = getattr(self.my.par, 'Aiprojectroot', None)
        mode = mode_par.eval() if mode_par is not None else 'gitroot'
        return self._rootForMode(mode)

    def _rootForMode(self, mode, custom_path=None):
        """Resolve a root directory for a given Aiprojectroot mode value.

        Used by _findProjectRoot() and by _migrateRootFiles() to compute
        both the old and new candidate roots when the parameter flips.

        custom_path: explicit override for 'custom' mode. When None and
        mode == 'custom', reads from the Aiprojectrootcustom parameter.
        Pass explicitly when computing the OLD root after a path change
        (parexec's prev value).
        """
        project_dir = Path(project.folder).resolve()
        if mode == 'projectfolder':
            return project_dir

        if mode == 'custom':
            if custom_path is None:
                custom_par = getattr(self.my.par, 'Aiprojectrootcustom', None)
                custom_path = custom_par.eval() if custom_par is not None else ''
            custom_path = (custom_path or '').strip()
            if not custom_path:
                # Empty custom path -- treat as projectfolder until user
                # picks one. Safer than picking a surprising fallback.
                return project_dir
            p = Path(custom_path)
            if not p.is_absolute():
                p = (project_dir / p).resolve()
            else:
                p = p.resolve()
            return p

        # gitroot: prefer the stored git root from Start/InitGit, else
        # walk up from project.folder looking for .git.
        git_root = self.my.fetch('_git_root', None, search=False)
        if git_root and git_root != 'no-git':
            return Path(git_root) if not isinstance(git_root, Path) else git_root

        # Walk up looking for .git. The home_dir guard prevents picking up
        # an unrelated repo (e.g. ~/.dotfiles) when project.folder is inside
        # the home directory. But only apply it when home_dir is actually
        # an ancestor - otherwise (e.g. a Windows project on D:\) the part-
        # count comparison wrongly bailed before searching at all (issue #19).
        try:
            home_dir = Path.home().resolve()
        except Exception:
            home_dir = None
        home_is_ancestor = bool(
            home_dir and (home_dir == project_dir or home_dir in project_dir.parents)
        )
        for parent_dir in [project_dir] + list(project_dir.parents):
            if home_is_ancestor and parent_dir == home_dir:
                break
            if (parent_dir / '.git').exists():
                return parent_dir
        return project_dir

    # Marker present in every file Embody writes through _writeTemplate.
    # Cleanup deletes only files containing this marker -- never touches
    # user-authored content that happens to share a path.
    _EMBODY_MARKER = '<!-- Generated by Embody/Envoy'

    def _atomicMove(self, src, dst):
        """Cross-filesystem-safe atomic move via copy-to-tmp + os.replace.

        Plain shutil.move falls back to copy+delete across filesystems --
        if interrupted mid-copy, dst may be a partial file. This helper
        copies to a sibling tmp file, then os.replace's it into place
        (atomic on a single filesystem), then unlinks src. A failed copy
        leaves only tmp behind; dst is never in a half-written state.

        Critical for palette catalog files (catalog_*.json), which are
        large and not regenerated from settings on next load.
        """
        import os, shutil
        src, dst = Path(src), Path(dst)
        tmp = dst.with_name(dst.name + '.embody-migrate-tmp')
        try:
            shutil.copy2(str(src), str(tmp))
            os.replace(str(tmp), str(dst))
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        try:
            src.unlink()
        except OSError as e:
            self.Log(
                f'Migration left source at {src}: {e}. The new copy at '
                f'{dst} is valid; remove the source manually.',
                'WARNING')

    def _migrateRootFiles(self, old_mode, new_mode,
                          old_custom=None, new_custom=None):
        """Relocate Embody/AI config when Aiprojectroot (or its custom
        path) flips.

        Three passes:
          1. Move Embody persistent state (.embody/config.json, project.json,
             and palette catalogs which are expensive to regenerate).
          2. Delete Embody-generated AI files at the old root that carry the
             marker, plus the regeneratable .embody/ runtime files. Files
             without the marker (e.g. user-authored .claude/skills/my-skill/)
             are left untouched.
          3. Surgically remove just the 'envoy' entry from the old .mcp.json
             so any other MCP servers the user configured stay intact.
          4. Prune empty Embody-owned directories.

        AI-tool-facing files are then regenerated at the new root by
        InitEnvoy() (called from parexec right after this method).

        old_custom/new_custom: explicit custom-path overrides. Used by
        parexec when Aiprojectrootcustom changes within 'custom' mode
        (both modes == 'custom' but the resolved paths differ).
        """
        old_root = self._rootForMode(old_mode, custom_path=old_custom)
        new_root = self._rootForMode(new_mode, custom_path=new_custom)
        if old_root == new_root:
            return

        # --- Pass 1: move Embody persistent state to new root ---
        moves = [old_root / '.embody' / 'config.json',
                 old_root / '.embody' / 'project.json']
        old_embody = old_root / '.embody'
        if old_embody.is_dir():
            moves.extend(sorted(old_embody.glob('catalog_*.json')))
        critical_srcs = [old_root / '.embody' / 'config.json',
                         old_root / '.embody' / 'project.json']
        for src in moves:
            if not src.is_file():
                continue
            rel = src.relative_to(old_root)
            dst = new_root / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.is_file():
                    src.unlink()
                    self.Log(f'Removed stale {rel} at {old_root}', 'DEBUG')
                else:
                    self._atomicMove(src, dst)
                    self.Log(f'Moved {rel} -> {new_root}', 'DEBUG')
            except Exception as e:
                self.Log(f'Could not migrate {rel}: {e}', 'WARNING')

        # Orphan handling: a failed move leaves the source in place. If
        # the critical settings file (config.json) is still at old_root
        # after Pass 1, rename it to .orphan so _findSettingsFile's
        # fallback doesn't pick up the stale data on the next restart.
        for orphan in critical_srcs:
            if orphan.is_file():
                backup = orphan.with_suffix(orphan.suffix + '.orphan')
                try:
                    orphan.rename(backup)
                    self.Log(
                        f'Migration left {orphan.relative_to(old_root)} '
                        f'at old root; renamed to {backup.name} so it does '
                        f'not interfere with future restores. Delete '
                        f'manually if no longer needed.',
                        'WARNING')
                except OSError as e:
                    self.Log(
                        f'Could not rename orphan {orphan}: {e}', 'WARNING')

        # Migrate .claude/settings.local.json separately: it has no marker
        # so cleanup would skip it (intentional -- it may contain user-added
        # MCP permissions). Moving it preserves those permissions across the
        # flip. If both locations have one, leave both alone (don't merge
        # blindly) and log so the user can reconcile manually.
        old_settings = old_root / '.claude' / 'settings.local.json'
        new_settings = new_root / '.claude' / 'settings.local.json'
        if old_settings.is_file():
            if new_settings.is_file():
                self.Log(
                    f'.claude/settings.local.json exists at both '
                    f'{old_root} and {new_root} -- keeping both. '
                    f'Merge manually if needed.',
                    'WARNING')
            else:
                try:
                    new_settings.parent.mkdir(parents=True, exist_ok=True)
                    self._atomicMove(old_settings, new_settings)
                    self.Log(
                        f'Moved .claude/settings.local.json -> {new_root}',
                        'INFO')
                except Exception as e:
                    self.Log(
                        f'Could not move .claude/settings.local.json: {e}',
                        'WARNING')

        # --- Pass 2: delete Embody-generated AI files at old root ---
        self._cleanupOldRootFiles(old_root)

        self.Log(
            f'AI config root: {old_mode} -> {new_mode}. '
            f'Old root {old_root} cleaned, regenerating at {new_root}.',
            'INFO')

    def _cleanupOldRootFiles(self, old_root):
        """Remove Embody-generated AI/MCP config files at the old root.

        Only deletes files containing the _EMBODY_MARKER comment (so any
        user-authored files at the same paths are preserved). Regeneratable
        runtime files in .embody/ are deleted unconditionally since they
        are 100% Embody-owned. .mcp.json is edited surgically to remove
        just the 'envoy' server entry. Empty Embody-owned directories are
        pruned after deletion.
        """
        deleted = 0

        def remove_if_marked(path):
            nonlocal deleted
            if not path.is_file():
                return
            try:
                content = path.read_text(encoding='utf-8', errors='ignore')
            except OSError as e:
                self.Log(f'Could not read {path}: {e}', 'WARNING')
                return
            if self._EMBODY_MARKER not in content:
                return
            try:
                path.unlink()
                deleted += 1
            except OSError as e:
                self.Log(f'Could not delete {path}: {e}', 'WARNING')

        # Top-level marker files
        for name in ('AGENTS.md', 'CLAUDE.md', 'ENVOY.md'):
            remove_if_marked(old_root / name)

        # Tree-scoped marker files: anything Embody writes via _writeTemplate
        for sub in ('.claude/rules', '.claude/skills',
                    '.cursor/rules',
                    '.github/instructions',
                    '.windsurf/rules'):
            d = old_root / sub
            if not d.is_dir():
                continue
            for p in d.rglob('*'):
                if p.is_file():
                    remove_if_marked(p)
        # Single-file marker location
        remove_if_marked(old_root / '.github' / 'copilot-instructions.md')

        # .embody/ runtime files (Embody-owned, no marker -- safe to remove).
        # The .envoy-tools-cache.json (hidden dot variant) never lived under
        # .embody/ but is listed here defensively in case a future bridge
        # version writes one.
        embody_dir = old_root / '.embody'
        if embody_dir.is_dir():
            for name in ('envoy.json', 'envoy-bridge.py',
                         'envoy-tools-cache.json',
                         '.envoy-tools-cache.json'):
                p = embody_dir / name
                if p.is_file():
                    try:
                        p.unlink()
                        deleted += 1
                    except OSError as e:
                        self.Log(f'Could not delete {p}: {e}', 'WARNING')

        # Legacy Embody-owned paths from prior versions. These migrated
        # away in newer Embody releases (see _configureMCPClient and
        # _restoreSettings migration blocks). Sweep them at the old root
        # so a flip-back from a long-lived install doesn't leave drift.
        legacy_paths = [
            old_root / '.claude' / 'envoy-bridge.py',     # moved to .embody/
            old_root / '.envoy-tools-cache.json',         # moved to .embody/
            old_root / '.envoy.json',                     # moved to .embody/envoy.json
            old_root / '.embody.json',                    # moved to .embody/config.json
        ]
        for legacy in legacy_paths:
            if legacy.is_file():
                try:
                    legacy.unlink()
                    deleted += 1
                except OSError as e:
                    self.Log(f'Could not delete legacy {legacy}: {e}', 'WARNING')

        # .mcp.json: remove only the 'envoy' server entry, preserve others
        mcp_file = old_root / '.mcp.json'
        if mcp_file.is_file():
            try:
                import json
                cfg = json.loads(mcp_file.read_text(encoding='utf-8'))
                servers = cfg.get('mcpServers', {})
                if 'envoy' in servers:
                    del servers['envoy']
                    if servers:
                        cfg['mcpServers'] = servers
                        mcp_file.write_text(
                            json.dumps(cfg, indent=2) + '\n',
                            encoding='utf-8')
                        self.Log(
                            f'Pruned envoy server from {mcp_file} '
                            f'(other servers preserved)',
                            'DEBUG')
                    else:
                        mcp_file.unlink()
                        deleted += 1
            except (json.JSONDecodeError, OSError) as e:
                self.Log(f'Could not clean old .mcp.json: {e}', 'WARNING')

        # Prune empty Embody-owned dirs (rmdir fails on non-empty -> safe).
        # Children-first so parents can empty as their leaves go.
        # First pass: sweep emptied skill/instruction subdirs.
        for parent in (old_root / '.claude' / 'skills',
                       old_root / '.github' / 'instructions'):
            if not parent.is_dir():
                continue
            for child in parent.iterdir():
                if child.is_dir():
                    try:
                        child.rmdir()
                    except OSError:
                        pass  # User content inside -- leave alone
        # Second pass: known top-level Embody-owned dirs.
        for d in (old_root / '.claude' / 'rules',
                  old_root / '.claude' / 'skills',
                  old_root / '.claude',
                  old_root / '.cursor' / 'rules',
                  old_root / '.cursor',
                  old_root / '.windsurf' / 'rules',
                  old_root / '.windsurf',
                  old_root / '.github' / 'instructions',
                  old_root / '.github',
                  old_root / '.embody'):
            try:
                if d.is_dir():
                    d.rmdir()
            except OSError:
                pass  # Not empty (user content remains) -- leave alone

        if deleted:
            self.Log(f'Removed {deleted} Embody-generated file(s) at {old_root}', 'INFO')

    def _extractAIConfig(self):
        """Extract AI coding assistant config files based on par.Aiclient."""
        target_dir = self._findProjectRoot()
        client = self.my.par.Aiclient.eval()

        # Always: AGENTS.md (universal standard, read by all major AI tools)
        self._writeAgentsMd(target_dir)

        if client == 'claudecode':
            self._writeClaudeCodeConfig(target_dir)
        elif client == 'cursor':
            self._writeCursorRules(target_dir)
        elif client == 'copilot':
            self._writeCopilotInstructions(target_dir)
        elif client == 'windsurf':
            self._writeWindsurfRules(target_dir)
        # 'none': AGENTS.md only (already written above)

    def _writeAgentsMd(self, target_dir):
        """Write AGENTS.md -- universal AI instructions read by all major AI tools."""
        templates_comp = self.my.op('templates')
        agents_md_dat = templates_comp.op('text_agents_md') if templates_comp else None

        if agents_md_dat and agents_md_dat.text:
            content = agents_md_dat.text
        else:
            # Assemble from the 3 rule templates as a fallback
            self.Log('text_agents_md DAT not found -- assembling AGENTS.md from rules', 'DEBUG')
            parts = ['<!-- Generated by Embody/Envoy -- do not edit manually -->\n']
            parts.append('# Embody + Envoy -- AI Instructions\n\n')
            parts.append(
                'This project uses [Embody](https://github.com/dylanroscover/Embody) '
                '(TouchDesigner externalization) and Envoy (MCP server for AI coding tools).\n\n'
                '---\n\n'
            )
            if templates_comp:
                for dat_name in self._TEMPLATE_MAP_RULES:
                    dat = templates_comp.op(dat_name)
                    if dat and dat.text:
                        # Strip frontmatter from each rule before embedding
                        parts.append(self._stripFrontmatter(dat.text).strip())
                        parts.append('\n\n---\n\n')
            content = ''.join(parts)

        self._writeTemplate(target_dir, 'AGENTS.md', content)

    def _writeClaudeCodeConfig(self, target_dir):
        """Write Claude Code config: CLAUDE.md + .claude/rules/ + .claude/skills/"""
        # 1. CLAUDE.md (with ENVOY.md fallback if user already has one)
        self._writeClaudeMd(target_dir)

        # 2. .claude/rules/ and .claude/skills/ from template DATs
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log('Templates COMP not found -- skipping .claude/ generation', 'DEBUG')
            return

        written = 0
        for dat_name, slug in self._TEMPLATE_MAP_RULES.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            # Claude Code doesn't use YAML frontmatter -- strip it.
            # Keep the generated-by marker for overwrite protection.
            content = self._stripFrontmatter(template_dat.text)
            if self._writeTemplate(target_dir, f'.claude/rules/{slug}.md', content):
                written += 1

        for dat_name, slug in self._TEMPLATE_MAP_SKILLS.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            if self._writeTemplate(target_dir, f'.claude/skills/{slug}/SKILL.md', template_dat.text):
                written += 1

        if written > 0:
            self.Log(f'Generated {written} .claude/ files at {target_dir}', 'SUCCESS')

    def _stripFrontmatter(self, content):
        """Strip leading YAML frontmatter (---...---) from content if present.

        Returns the content after the closing --- block, with leading whitespace
        trimmed. Handles BOM-prefixed content.
        """
        # Strip BOM that TD may add to externalized files
        content = content.lstrip('\ufeff')
        if not content.startswith('---\n'):
            return content
        close_idx = content.find('\n---\n', 4)
        if close_idx == -1:
            return content
        return content[close_idx + 5:].lstrip('\n')

    def _writeCursorRules(self, target_dir):
        """Write Cursor rules: .cursor/rules/{slug}.mdc with YAML frontmatter.

        Templates already embed a 'description:' field. This injects 'globs: []'
        and 'alwaysApply: true' into the existing frontmatter rather than
        prepending a duplicate block.
        """
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log('Templates COMP not found -- skipping .cursor/ generation', 'DEBUG')
            return

        written = 0
        for dat_name, slug in self._TEMPLATE_MAP_RULES.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            raw = template_dat.text.lstrip('\ufeff')
            # Inject globs/alwaysApply into existing frontmatter
            SEP = '\n---\n'
            if raw.startswith('---\n') and SEP in raw[4:]:
                close_idx = raw.find(SEP, 4)
                fm_lines = raw[4:close_idx]
                rest = raw[close_idx + len(SEP):]
                if 'alwaysApply:' not in fm_lines:
                    fm_lines += '\nglobs: []\nalwaysApply: true'
                content = '---\n' + fm_lines + SEP + rest
            else:
                # No frontmatter -- build one from first H1
                description = slug.replace('-', ' ').title()
                for line in raw.splitlines():
                    if line.startswith('# '):
                        description = line[2:].strip()
                        break
                content = (
                    f'---\ndescription: "{description}"\n'
                    f'globs: []\nalwaysApply: true\n---\n\n{raw}'
                )
            if self._writeTemplate(target_dir, f'.cursor/rules/{slug}.mdc', content):
                written += 1

        if written > 0:
            self.Log(f'Generated {written} .cursor/rules/ files at {target_dir}', 'SUCCESS')

    def _writeCopilotInstructions(self, target_dir):
        """Write GitHub Copilot config: combined instructions + per-rule files."""
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log('Templates COMP not found -- skipping .github/ generation', 'DEBUG')
            return

        written = 0
        rule_parts = ['<!-- Generated by Embody/Envoy -- do not edit manually -->\n\n']
        individual_contents = {}

        for dat_name, slug in self._TEMPLATE_MAP_RULES.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            # Strip template frontmatter -- Copilot uses its own applyTo format
            rule_content = self._stripFrontmatter(template_dat.text).strip()
            # Extract heading for section label
            heading = slug.replace('-', ' ').title()
            for line in rule_content.splitlines():
                if line.startswith('# '):
                    heading = line[2:].strip()
                    break
            rule_parts.append(f'## {heading}\n\n{rule_content}\n\n---\n\n')
            # Individual file with applyTo frontmatter + generated marker
            individual_contents[slug] = (
                f'---\n'
                f'applyTo: "**"\n'
                f'---\n\n'
                f'<!-- Generated by Embody/Envoy -- do not edit manually -->\n\n'
                f'{rule_content}'
            )

        # Combined file (.github/copilot-instructions.md)
        combined = ''.join(rule_parts)
        if self._writeTemplate(target_dir, '.github/copilot-instructions.md', combined):
            written += 1

        # Individual per-rule files (.github/instructions/{slug}.instructions.md)
        for slug, content in individual_contents.items():
            if self._writeTemplate(target_dir, f'.github/instructions/{slug}.instructions.md', content):
                written += 1

        if written > 0:
            self.Log(f'Generated {written} .github/ files at {target_dir}', 'SUCCESS')

    def _writeWindsurfRules(self, target_dir):
        """Write Windsurf rules: .windsurf/rules/{slug}.md (plain markdown)."""
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log('Templates COMP not found -- skipping .windsurf/ generation', 'DEBUG')
            return

        written = 0
        for dat_name, slug in self._TEMPLATE_MAP_RULES.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            if self._writeTemplate(target_dir, f'.windsurf/rules/{slug}.md', template_dat.text):
                written += 1

        if written > 0:
            self.Log(f'Generated {written} .windsurf/rules/ files at {target_dir}', 'SUCCESS')

    def _writeClaudeMd(self, target_dir):
        """Write CLAUDE.md from the text_claude template DAT."""
        templates_comp = self.my.op('templates')
        template_dat = templates_comp.op('text_claude') if templates_comp else None
        if not template_dat:
            self.Log('CLAUDE.md template DAT not found inside Embody/templates', 'WARNING')
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
        """Silently extract AI config if Envoy is enabled but files are missing."""
        if not self.my.par.Envoyenable.eval():
            return
        target_dir = self._findProjectRoot()
        client = self.my.par.Aiclient.eval()
        agents_md_missing = not (target_dir / 'AGENTS.md').exists()
        if agents_md_missing or self._clientFilesMissing(target_dir, client):
            self._extractAIConfig()

    def _clientFilesMissing(self, target_dir, client):
        """Return True if the primary config files for the selected client are absent."""
        checks = {
            'claudecode': lambda d: (
                not (d / 'CLAUDE.md').exists() and not (d / 'ENVOY.md').exists()
            ) or not (d / '.claude' / 'rules').exists(),
            'cursor':     lambda d: not (d / '.cursor' / 'rules').exists(),
            'copilot':    lambda d: not (d / '.github' / 'copilot-instructions.md').exists(),
            'windsurf':   lambda d: not (d / '.windsurf' / 'rules').exists(),
            'none':       lambda d: False,
        }
        return checks.get(client, lambda d: False)(target_dir)

    def InitEnvoy(self) -> None:
        """(Re)generate all Envoy and AI client config files.

        Writes MCP config (.mcp.json, .embody/envoy.json, bridge script,
        settings.local.json) and AI client files (CLAUDE.md, AGENTS.md,
        .claude/rules/, .claude/skills/, or equivalent for Cursor/Copilot/
        Windsurf) to the git root or project folder.

        Safe to call at any time -- idempotent. Use this after initializing
        a git repo, changing the AI client setting, or updating Embody to
        refresh generated files.

        Requires Envoy to be enabled (par.Envoyenable = True).
        """
        if not self.my.par.Envoyenable.eval():
            self.Log('Envoy is not enabled. Set Envoyenable = True first.', 'WARNING')
            return

        target_dir = self._findProjectRoot()

        # MCP config (port comes from the running server, or the parameter)
        envoy = self.my.ext.Envoy
        if self.my.fetch('envoy_running', False):
            # Extract port from current status string
            status = str(self.my.par.Envoystatus.eval())
            import re
            match = re.search(r'port\s+(\d+)', status)
            port = int(match.group(1)) if match else self.my.par.Envoyport.eval()
        else:
            port = self.my.par.Envoyport.eval()

        envoy._configureMCPClient(port, target_dir=target_dir)

        # AI client config (CLAUDE.md, AGENTS.md, rules, skills, etc.)
        self._extractAIConfig()

        client_label = self.my.par.Aiclient.label
        self.Log(
            f'Envoy config regenerated for {client_label} at {target_dir}',
            'SUCCESS')

    def InitGit(self) -> None:
        """Initialize or reconnect to a git repository, then generate
        git-related config files (.gitignore, .gitattributes).

        If no git repo exists, prompts the user to initialize one.
        After git is available, also regenerates MCP and AI client config
        so paths point to the git root.

        Safe to call at any time. Use this after creating a git repo
        manually, or to refresh .gitignore/.gitattributes entries.

        Requires Envoy to be enabled (par.Envoyenable = True).
        """
        if not self.my.par.Envoyenable.eval():
            self.Log('Envoy is not enabled. Set Envoyenable = True first.', 'WARNING')
            return

        envoy = self.my.ext.Envoy
        git_root = envoy._checkOrInitGitRepo()

        if git_root is None:
            return  # User cancelled

        if git_root == 'no-git':
            self.Log('No git repo -- .gitignore/.gitattributes skipped.', 'INFO')
            return

        # Store git root so Envoy can find it later (e.g. for deregistration)
        self.my.store('_git_root', git_root)

        # Git-specific config
        envoy._configureGitignore(git_root)
        envoy._configureGitattributes(git_root)
        self.Log(f'Git config generated at {git_root}', 'SUCCESS')

        # Regenerate MCP + AI config so paths point to git root
        self.InitEnvoy()

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

        # Update scenario: par reference is lost but the sibling table survived
        # Embody deletion (undocked tables are not deleted with their host).
        if not externalizations_dat:
            existing_sibling = self.my.parent().op(table_name)
            if existing_sibling and existing_sibling.family == 'DAT':
                externalizations_dat = existing_sibling
                self.my.par.Externalizations.val = externalizations_dat
                self.Log(f"Re-connected to existing '{table_name}' tableDAT", "INFO")

        if not externalizations_dat:
            # Truly fresh install -- create new table as a regular sibling.
            # NOTE: not docked to Embody so the table survives when Embody is
            # deleted during an upgrade (delete old -> drag new .tox).
            externalizations_dat = self.my.parent().create(tableDAT, table_name)
            externalizations_dat.nodeX = self.my.nodeX - 200
            externalizations_dat.nodeY = self.my.nodeY
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

    def CreateExternalizationsTable(self) -> None:
        """Recovery/init method: create or reconnect the externalizations table.

        Safe to call at any time. No-op if the table already exists and is
        connected via par.Externalizations. If the parameter is empty but a
        sibling named 'externalizations' exists (e.g. after an Embody upgrade),
        reconnects to it without creating a duplicate.
        """
        externalizations_dat = self.Externalizations
        if not externalizations_dat:
            existing_sibling = self.my.parent().op('externalizations')
            if existing_sibling and existing_sibling.family == 'DAT':
                self.my.par.Externalizations.val = existing_sibling
                self.Log('Re-connected to existing externalizations tableDAT', 'INFO')
                return
        if externalizations_dat:
            self.Log('Externalizations table already exists', 'INFO')
            return
        self.createExternalizationsTable()

    def _migrateTableSchema(self) -> None:
        """Migrate externalizations table schema to current version.

        Adds missing columns (strategy, node_x, node_y, node_color),
        populates them from existing data, and removes legacy rows.
        """
        table = self.Externalizations
        if not table or table.numRows < 1:
            return

        headers = [self._cellVal(0, c) for c in range(table.numCols)]

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
                row_type = self._cellVal(i, 'type')
                rel_path = self._cellVal(i, 'rel_file_path')

                if row_type == 'tdn':
                    rows_to_delete.append(i)
                    continue

                oper = op(self._cellVal(i, 'path'))
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
            headers = [self._cellVal(0, c) for c in range(table.numCols)]

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

    @staticmethod
    def _resolveOsLabel(os_name: str, os_version: str, win_build) -> str:
        """Pure OS-label resolution, isolated from TD globals for testability.

        TouchDesigner's ``app.osVersion`` reports ``"10"`` on Windows 11 -- both
        Windows 10 and 11 share NT kernel version 10.0, so the only reliable
        discriminator is the build number: 22000+ means Windows 11. ``win_build``
        is ``sys.getwindowsversion().build`` (an int), or ``None`` when that
        probe is unavailable (i.e. not running on Windows). On macOS / genuine
        Windows 10 the label passes through unchanged.
        """
        label = f'{os_name} {os_version}'.strip()
        if 'Windows' in os_name and '11' not in label:
            if win_build is not None and win_build >= 22000:
                label = 'Windows 11'
        return label

    @staticmethod
    def _osLabel() -> str:
        """Human-readable OS label for logs and diagnostics, fixed for Win 11.

        See _resolveOsLabel for why this can't just trust app.osName/osVersion.
        """
        try:
            win_build = sys.getwindowsversion().build
        except (AttributeError, OSError):
            win_build = None  # Not Windows, or the probe isn't available.
        return EmbodyExt._resolveOsLabel(app.osName, app.osVersion, win_build)

    # ==========================================================================
    # SETTINGS PERSISTENCE
    # ==========================================================================

    def _settingsPath(self) -> Path:
        """Path to .embody/config.json -- consistent with _findProjectRoot()."""
        return self._findProjectRoot() / '.embody' / 'config.json'

    def _findSettingsFile(self) -> Optional[Path]:
        """Locate .embody/config.json, checking both Aiprojectroot candidate
        roots.

        At TD launch, _restoreSettings() runs before any param values have
        been restored -- so Aiprojectroot sits at its baked-in default
        ('gitroot'). If the user previously flipped to 'projectfolder',
        their config.json lives at the project folder, not git root. The
        canonical _settingsPath() lookup would miss it and silently bail,
        losing every persisted setting on every restart.

        This helper resolves that chicken-and-egg by trying both candidate
        roots before declaring the file absent. Returns the path if found,
        else None.
        """
        canonical = self._settingsPath()
        if canonical.is_file():
            return canonical
        # Try the alternate predefined modes (gitroot, projectfolder).
        for mode in ('gitroot', 'projectfolder'):
            alt = self._rootForMode(mode) / '.embody' / 'config.json'
            if alt != canonical and alt.is_file():
                self.Log(
                    f'config.json found at alternate root (Aiprojectroot '
                    f'will be restored from saved value): {alt}',
                    'INFO')
                return alt
        # Last-resort walk-up from project.folder. Catches the 'custom'
        # mode chicken-and-egg: the saved custom path lives in
        # config.json which we haven't read yet, so we can't compute the
        # canonical custom path. Walking up from the .toe finds any
        # .embody/config.json a user previously put on the tree.
        project_dir = Path(project.folder).resolve()
        for parent_dir in project_dir.parents:
            candidate = parent_dir / '.embody' / 'config.json'
            if candidate == canonical:
                continue
            if candidate.is_file():
                self.Log(
                    f'config.json found by ancestor walk-up: {candidate}',
                    'INFO')
                return candidate
        return None

    def _projectJsonPath(self) -> Path:
        """Path to .embody/project.json -- committed project metadata.

        Unlike .embody/config.json (user-local settings) and .embody/envoy.json
        (live runtime registry), project.json is intended to be checked into git
        so the same metadata travels with the repo to every machine.
        """
        return self._findProjectRoot() / '.embody' / 'project.json'

    def _writeProjectJson(self) -> None:
        """Pin the current TouchDesigner build into .embody/project.json.

        The Envoy bridge reads td_build to pick a matching TD install when
        launching on a fresh clone, where envoy.json is gitignored and its
        td_executable path may not exist locally. Idempotent -- skips the
        write when td_build is already current.
        """
        import json, os
        path = self._projectJsonPath()
        # app.build is the build proper (e.g. '2025.32460'). app.version is
        # the long-lived major branch ('099') and would only be noise here.
        current_build = app.build

        existing = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    existing = loaded
            except (json.JSONDecodeError, OSError):
                pass  # Treat unreadable as empty -- we'll overwrite.

        if existing.get('td_build') == current_build:
            return

        existing['td_build'] = current_build

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(path) + '.tmp')
            content = json.dumps(existing, indent=2) + '\n'
            for attempt in range(3):
                try:
                    tmp.write_text(content, encoding='utf-8')
                    os.replace(str(tmp), str(path))
                    self.Log(
                        f'Pinned td_build={current_build} in '
                        f'.embody/project.json',
                        'DEBUG')
                    return
                except PermissionError:
                    if attempt < 2:
                        import time as _time
                        _time.sleep(0.1)
                    else:
                        raise
        except Exception as e:
            self.Log(f'Failed to write project.json: {e}', 'WARNING')

    def _saveSettings(self) -> None:
        """Persist whitelisted parameter values to .embody/config.json."""
        self._settings_save_pending = False
        params = {}
        # Sort names so JSON output is stable across TD sessions. _PERSISTED_PARAMS
        # is a frozenset, and Python's hash randomization gives each process a
        # different iteration order -- producing noisy diffs on every save.
        for name in sorted(self._PERSISTED_PARAMS):
            par = getattr(self.my.par, name, None)
            if par is None:
                continue
            entry = {'val': par.eval()}
            if par.mode != ParMode.CONSTANT:
                entry['mode'] = str(par.mode)
                if par.expr:
                    entry['expr'] = par.expr
                if par.bindExpr:
                    entry['bindExpr'] = par.bindExpr
            params[name] = entry
        data = {'version': 1, 'params': params}
        try:
            import json, os
            path = self._settingsPath()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(path) + '.tmp')
            content = json.dumps(data, indent=2, sort_keys=True) + '\n'
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
        except Exception as e:
            self.Log(f'Failed to save settings: {e}', 'WARNING')

    def _deferSaveSettings(self) -> None:
        """Schedule a settings save on the next frame. Coalesces rapid changes."""
        if not getattr(self, '_settings_save_pending', False):
            self._settings_save_pending = True
            run(f"op('{self.my}').ext.Embody._saveSettings()", delayFrames=1)

    def _restoreSettings(self, kick_envoy: bool = False) -> bool:
        """Restore parameter values from .embody/config.json. Returns True if restored.
        Sets _restoring_settings flag to suppress onValueChange side effects.

        Also stores _init_complete when done -- init() no longer stores it because
        TD defers onValueChange callbacks to the next cook, and storing _init_complete
        in init() allowed parexec to process init()'s Envoyenable=False change.

        kick_envoy: if True and Envoyenable is restored to True, defer Start().
        Only set this on the onStart() path -- Verify() owns startup on onCreate()."""
        # _findSettingsFile handles the Aiprojectroot chicken-and-egg: at
        # this point Aiprojectroot is at its baked-in default, so a saved
        # value of 'projectfolder' wouldn't resolve via _settingsPath alone.
        path = self._findSettingsFile()
        if path is None:
            # Migrate: check old root-level .embody.json
            canonical = self._settingsPath()
            old_path = self._findProjectRoot() / '.embody.json'
            if old_path.is_file():
                try:
                    canonical.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.move(str(old_path), str(canonical))
                    self.Log('Migrated .embody.json -> .embody/config.json', 'INFO')
                    path = canonical
                except Exception as e:
                    self.Log(f'Could not migrate .embody.json: {e}', 'WARNING')
                    self.my.store('_init_complete', True)
                    return False
            else:
                self.my.store('_init_complete', True)
                return False
        try:
            import json
            data = json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            self.Log(f'Settings file corrupt or unreadable: {e}', 'WARNING')
            self.my.store('_init_complete', True)
            return False
        if not isinstance(data, dict) or 'params' not in data:
            self.my.store('_init_complete', True)
            return False
        params = data['params']
        restored = 0
        self._restoring_settings = True
        try:
            for name, entry in params.items():
                par = getattr(self.my.par, name, None)
                if par is None or name not in self._PERSISTED_PARAMS:
                    continue
                try:
                    mode = entry.get('mode')
                    if mode and 'expr' in entry:
                        par.expr = entry['expr']
                    elif mode and 'bindExpr' in entry:
                        par.bindExpr = entry['bindExpr']
                    else:
                        par.val = entry['val']
                    restored += 1
                except Exception:
                    pass
        finally:
            self._restoring_settings = False
        # Signal parexec that init + restore is complete -- safe to process
        # param changes.  Must be stored AFTER _restoring_settings is cleared
        # so deferred onValueChange callbacks from init() are still suppressed.
        self.my.store('_init_complete', True)
        self.Log(f'Restored {restored} settings from config.json', 'INFO')
        # TDN mode migration detection: an upgrading user will have
        # 'Tdnenable' in their persisted params but not 'Tdnmode'. Defer
        # the nudge dialog so init can complete cleanly first.
        # Guarded by a schedule-time flag so a second _restoreSettings in
        # the same session (e.g. onCreate then onStart) can't queue a
        # second dialog before the first one fires.
        already_scheduled = self.my.fetch(
            '_tdn_migration_scheduled', False, search=False)
        if ('Tdnenable' in params and 'Tdnmode' not in params
                and not already_scheduled):
            prev_tdn_enable = bool(params.get('Tdnenable', {}).get('val', True))
            self.my.store('_tdn_migration_prev_enable', prev_tdn_enable)
            self.my.store('_tdn_migration_scheduled', True)
            run(f"op('{self.my}').ext.Embody._showTDNMigrationNudge()",
                delayFrames=60)
        # If Envoyenable was restored to True, kick Start() -- parexec was
        # suppressed during restore so onValueChange never fired.
        # Only set this on the onStart() path (kick_envoy=True).
        # Verify() owns Envoy startup on the onCreate() path.
        if kick_envoy and self.my.par.Envoyenable.eval():
            run(f"op('{self.my}').ext.Envoy.Start()", delayFrames=3)
        return restored > 0

    def _showTDNMigrationNudge(self) -> None:
        """One-time dialog after upgrading from the binary Tdnenable toggle.

        Fires when a user opens a project previously saved with the old
        Tdnenable toggle and no Tdnmode selection yet. Offers a choice
        between restoring Full bidirectional sync (their prior behavior)
        or adopting the new Export-on-Save default (recommended).

        Guarded by _tdn_mode_migration_shown so it only fires once per
        project across sessions (the flag is persisted via param write
        into config.json on next save).
        """
        if self.my.fetch('_tdn_mode_migration_shown', False, search=False):
            return
        prev_enable = self.my.fetch('_tdn_migration_prev_enable', True,
                                    search=False)
        self.my.unstore('_tdn_migration_prev_enable')

        tdn_comps = []
        try:
            tdn_comps = self._getTDNStrategyComps()
        except Exception:
            pass

        if not tdn_comps:
            # No TDN COMPs tracked -- silently accept the new default.
            self.my.store('_tdn_mode_migration_shown', True)
            return

        prev_label = ('Full (bidirectional)' if prev_enable
                      else 'Off (TDN disabled)')
        msg = (
            f'TDN default changed in this release.\n\n'
            f'Your project was previously saved with the legacy Tdnenable '
            f'toggle ({prev_label}). The new system has three modes:\n\n'
            f'  \u2022 Off -- no TDN runtime\n'
            f'  \u2022 Export-on-Save -- recommended; .toe is truth, '
            f'.tdn files are rewritten on save\n'
            f'  \u2022 Roundtrip (Experimental) -- bidirectional '
            f'strip/restore on save and reconstruction on open (previous '
            f'behavior)\n\n'
            f'Currently set to Export-on-Save. Your {len(tdn_comps)} '
            f'tracked TDN COMP(s) will stop round-tripping on save.\n\n'
            f'Keep the new default, or restore Full?'
        )
        choice = self._messageBox(
            'Embody - TDN Mode Changed',
            msg,
            buttons=['Keep Export-on-Save (recommended)',
                     'Restore Full (previous behavior)'])
        if choice == 1:
            try:
                self.my.par.Tdnmode = 'full'
                self._applyTdnModeGating()
                self.Log('TDN mode restored to Full per user choice', 'INFO')
            except Exception as e:
                self.Log(f'Could not restore Full mode: {e}', 'WARNING')
        else:
            self.Log('TDN mode kept at Export-on-Save (new default)', 'INFO')
        self.my.store('_tdn_mode_migration_shown', True)

    def Verify(self) -> None:
        """Initialize or reconnect Embody on install or update.

        Called from execute.py onCreate() after CreateExternalizationsTable()
        has already run.  Two scenarios:

        - Fresh install: table exists but is empty (just created) -- skip dialog,
          run UpdateHandler quietly, then offer Envoy opt-in.
        - Update install: table has prior data -- offer a re-scan to validate
          tracked operators after upgrading Embody.
        """
        # Restore saved settings from a previous install before any dialogs.
        settings_restored = self._restoreSettings()

        embodies = op('/').findChildren(name='Embody', parName='Addtagshort')
        other_embody = next((e for e in embodies if e != self.my), None)

        if other_embody:
            self._messageBox('Embody',
                f'An instance of Embody already exists:\n{other_embody}\n'
                'Please remove it first.', buttons=['Ok'])
            return

        table = self.Externalizations
        has_prior_data = table and table.numRows > 1

        if has_prior_data:
            # UPDATE scenario: reconnected to a surviving table with prior entries.
            # Offer a re-scan so Embody validates/updates all tracked operators.
            choice = self._messageBox('Embody',
                f'{table.numRows - 1} externalized operator(s) found.\n\n'
                'Re-scan to validate tracked operators?\n'
                '(Recommended after upgrading Embody)',
                buttons=['Skip', 'Re-scan'])
            if choice in (1,):  # Re-scan
                self.Reset()
        else:
            # FRESH INSTALL: table was just created (empty). No dialog needed --
            # just run UpdateHandler quietly; it will find nothing yet.
            run(f"op('{self.my}').UpdateHandler()", delayFrames=10)

        # Defer Envoy opt-in until after the full init/update cycle completes.
        if settings_restored and has_prior_data:
            # Returning user: settings exist AND table has prior data -- this is
            # a genuine re-install or upgrade into an established project. Skip
            # the prompt; kick Envoy start if the restored settings have it
            # enabled (onValueChange was suppressed during restore).
            if self.my.par.Envoyenable.eval():
                # Longer delay on the upgrade path (onCreate -> Verify) to give
                # the old server thread time to release its port.  onDestroyTD
                # signals the old shutdown_event, but uvicorn can take 1-3s to
                # fully close its listener socket.  delayFrames=10 (~0.17s) was
                # too short, causing EADDRINUSE -> auto-restart exhaustion ->
                # Envoyenable stuck.  60 frames (~1s) is a safer window.
                run(f"op('{self.my}').ext.Envoy.Start()", delayFrames=60)
        else:
            # Fresh install (empty table). Always prompt -- even if a leftover
            # config.json from a previous install in the same folder was
            # restored, the user must explicitly opt in for this new project.
            # Reset Envoyenable so the prompt is the gate, not old settings.
            # Idempotent: do NOT re-queue if a prompt is already pending.
            # Tests that run multiple Verify() cycles in succession (e.g.
            # test_custom_parameters' Disable/Enable suite) would otherwise
            # stack N prompts, each one consuming one seeded auto-response
            # and the rest hitting ui.messageBox for real -- freezing TD
            # with modal dialogs the moment the test finishes.
            if not getattr(self, '_pending_envoy_prompt', False):
                self.my.par.Envoyenable = False
                self._pending_envoy_prompt = True

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
            rel_file_path = self._cellVal(i, 'rel_file_path')
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
        # SAFETY: Never clean directories outside the externalization folder.
        # When prevFolder is empty, folder falls back to project.folder -- which
        # is far too broad and can delete unrelated empty directories (issue #3).
        if folder and folder != project.folder:
            self._cleanupEmptyDirectories(folder, prevFolder)

        # Clear externalizations table synchronously (no delay -- delayed clear
        # creates a race condition if re-enabled before the callback fires)
        if self.Externalizations:
            self.Externalizations.clear(keepFirstRow=True)

        self.my.par.Status = 'Disabled'

        # Schedule deferred empty-dir cleanup only for the specific externalization
        # folder -- never for project.folder or empty paths (prevents deleting
        # newly-created target folders when changing the Folder parameter).
        if folder and folder != project.folder:
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
            
        # Remove empty top-level comp directories (skip SCM dirs)
        for comp in self.root.findChildren(depth=1, type=COMP):
            if comp.name in self._SCM_DIRS or comp.name in ['local', 'perform']:
                continue
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

        # Try to remove main externalization folder only if empty
        # SAFETY: Never remove project.folder itself
        try:
            if folder:
                folder_path = Path(folder).resolve()
                project_path = Path(project.folder).resolve()
                if folder_path != project_path and folder_path.is_dir():
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
            
            # Create externalization folder (makedirs handles missing parents)
            folder = self.getProjectFolder()
            try:
                os.makedirs(folder, exist_ok=True)
                self.Log(f"Created folder '{folder}'", "SUCCESS")
            except Exception as e:
                self.Log(f"Failed to create folder '{folder}': {e}", "ERROR")

        # Migrate table schema if needed (adds strategy column)
        self._migrateTableSchema()

        # Normalize paths for cross-platform compatibility
        self.normalizeAllPaths()

        # Apply UI gating for the TDN mode menu (greys out dependent
        # parameters based on Off / Export / Full).
        self._applyTdnModeGating()

        run(f"op('{self.my}').Update()", delayFrames=1)

    def normalizeAllPaths(self) -> None:
        """Normalize all paths in table and on operators for cross-platform support."""
        if not self.Externalizations:
            return
            
        paths_fixed = 0
        for i in range(1, self.Externalizations.numRows):
            rel_file_path = self._cellVal(i, 'rel_file_path')
            normalized = self.normalizePath(rel_file_path)

            if rel_file_path != normalized:
                self.Externalizations[i, 'rel_file_path'] = normalized
                paths_fixed += 1

            # Update operator parameter if needed
            op_path = self._cellVal(i, 'path')
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
        # Skip ONLY when Embody is explicitly Disabled. Status takes other
        # transient values during normal operation -- 'Scanning defaults (X/N)'
        # and 'Scanning palette (X/N)' from CatalogManager.EnsureCatalogs(),
        # 'Testing' from EnvoyExt port-test -- and Update must still run during
        # those windows. The previous `!= 'Enabled'` check raced with the
        # catalog scan that fires on fresh-project drops: the scan started
        # one frame before Update was scheduled, set Status to 'Scanning
        # defaults (0/N)', and Update returned early -- never consuming
        # _pending_envoy_prompt, so the Envoy opt-in dialog never appeared.
        if self.my.par.Status == 'Disabled':
            return
        if self._performMode:
            return

        # Detect a .toe basename change since the last Update and
        # propagate to the envoy.json registry. This is a defensive
        # backstop for execute.py's onProjectPostSave RefreshRegistry
        # call -- if execute.py wasn't reloaded after a source edit,
        # or the save took an Off/Export path that skipped Envoy
        # restart, this catches the rename on the next Update tick.
        # Idempotent: _writeEnvoyConfig short-circuits when the
        # registry is already current.
        try:
            current_name = project.name
            if getattr(self, '_last_toe_name', None) != current_name:
                self._last_toe_name = current_name
                if self.my.par.Envoyenable.eval():
                    self.my.ext.Envoy.RefreshRegistry()
        except Exception as e:
            self.Log(f'registry rename-detect failed: {e}', 'WARNING')

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

        # TDN-strategy COMP dirty detection + export is handled once, below,
        # by dirtyHandler(True) -- a single fingerprint sweep per Refresh that
        # covers both structural and authored-parameter changes. (It was
        # previously done here AND in dirtyHandler, fingerprinting every TDN
        # COMP twice per Refresh and dropping frames on large networks.)
        # tdn_paths is still gathered here so the "subtractions" filter below
        # continues to exclude tracked TDN COMPs.
        tdn_comps = self.getExternalizedOps(COMP, strategy='tdn')
        tdn_paths = {comp.path for comp in tdn_comps}
        if not self._tdnEnabled() and tdn_comps:
            self.Log(
                f'TDN disabled -- skipping export for {len(tdn_comps)} '
                f'tracked TDN COMP(s)', 'INFO')

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

        # TDN-strategy COMPs are excluded -- their lifecycle is managed by
        # ToggleTag() -> _removeTDNStrategy(), not by tag-presence detection.
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

        # Chain the Envoy opt-in prompt AFTER init completes.
        # Verify() sets this flag; we consume it here so the Envoy dialog
        # appears only after deprecated-pattern and re-scan dialogs resolve.
        if getattr(self, '_pending_envoy_prompt', False):
            self._pending_envoy_prompt = False
            run(f"op('{self.my}').ext.Embody._promptEnvoy()", delayFrames=5)

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
        if self._performMode:
            return
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
        # Collect externalization tag values, excluding the exclude-tag
        # parameter by NAME (not value). The exclude tag is not an
        # externalization tag -- it marks COMPs the TDN system must ignore --
        # so it must never reach a selector that drives DAT/COMP
        # externalization. Filtering by name (not value) means a user who
        # names the exclude tag identically to a real tag can't silently drop
        # that real tag. _hasExcludeTag (TDNExt) reads the par directly.
        tags = [par.eval() for par in self.my.pars('*tag')
                if par.name != 'Tdnexcludetag']
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
            strategy: Optional filter -- 'tox', 'tdn', or None for all.
        """
        if not self.Externalizations:
            return []

        family_str = 'COMP' if opFamily == COMP else 'DAT'
        has_strategy_col = 'strategy' in [
            self._cellVal(0, c)
            for c in range(self.Externalizations.numCols)
        ]
        ops = []

        for i in range(1, self.Externalizations.numRows):
            # Filter by strategy if requested
            if has_strategy_col and strategy:
                row_strategy = self._cellVal(i, 'strategy')
                if row_strategy != strategy:
                    continue
            elif not has_strategy_col:
                # Legacy table without strategy column -- skip TDN rows
                if self._cellVal(i, 'type') == 'tdn':
                    continue

            path = self._cellVal(i, 'path')
            if not path:
                continue
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
        """True if oper or any ancestor COMP is an active clone instance.

        A COMP whose par.clone self-references (a common pattern for
        reusable UI components using iop.* expressions) is treated as
        a master, not a clone.
        """
        p = oper
        while p is not None and p.path != '/':
            if p.family == 'COMP':
                clone_par = getattr(p.par, 'clone', None)
                enable_par = getattr(p.par, 'enablecloning', None)
                if clone_par is not None and enable_par is not None:
                    try:
                        clone_val = clone_par.eval()
                        if (clone_val and clone_val is not p
                                and enable_par.eval()):
                            return True
                    except Exception:
                        pass
            p = p.parent()
        return False

    def isClone(self, oper: OP) -> bool:
        """Check if operator is a clone COMP (not master).

        A COMP whose par.clone self-references is treated as a master.
        """
        if oper.family != 'COMP':
            return False
        clone_par = getattr(oper.par, 'clone', None)
        enable_par = getattr(oper.par, 'enablecloning', None)
        if clone_par is None or enable_par is None:
            return False
        try:
            clone_val = clone_par.eval()
            if clone_val and clone_val is not oper and enable_par.eval():
                return True
        except Exception:
            pass
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
        if self._performMode:
            return
        try:
            oper = op(opPath)
            if not oper or oper.family != 'COMP':
                self.Log(f"Save() requires a COMP, got {oper.family if oper else 'None'}: {opPath}", "ERROR")
                return
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
        if self._performMode:
            return
        if not self._tdnEnabled():
            self.Log(f'TDN disabled -- skipping SaveTDN for {opPath}', 'INFO')
            return
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
                raw_name = project.name.removesuffix('.toe')
                safe_name = self.my.ext.TDN._stripBuildSuffix(raw_name)
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

            # Export TDN -- protect .tdn files belonging to OTHER tracked
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

        # Phase 1: Collect file references and externalization params to strip.
        # Include the target itself -- its externaltox/enableexternaltox would
        # be baked into the .tox and confuse recipients.
        saved_state = []

        for op_ref in [target] + target.findChildren():
            if op_ref.family == 'DAT' and hasattr(op_ref.par, 'file'):
                file_val = op_ref.par.file.eval()
                sync_val = op_ref.par.syncfile.eval()
                if not file_val and not sync_val:
                    continue
                if file_val and (file_val.startswith('/') or (len(file_val) > 1 and file_val[1] == ':')):
                    # Absolute path -- warn if not a TD system path
                    if not file_val.startswith('/sys/'):
                        self.Log(
                            f"Absolute path won't be portable: "
                            f"{op_ref.path} -> {file_val}", "WARNING")
                else:
                    saved_state.append({
                        'op': op_ref,
                        'family': 'DAT',
                        'file': file_val,
                        'file_readonly': op_ref.par.file.readOnly,
                        'syncfile': sync_val,
                    })

            elif op_ref.family == 'COMP' and hasattr(op_ref.par, 'externaltox'):
                tox_val = op_ref.par.externaltox.eval()
                enable_val = op_ref.par.enableexternaltox.eval()
                if not tox_val and not enable_val:
                    continue
                if tox_val and (tox_val.startswith('/') or (len(tox_val) > 1 and tox_val[1] == ':')):
                    if not tox_val.startswith('/sys/'):
                        self.Log(
                            f"Absolute path won't be portable: "
                            f"{op_ref.path} -> {tox_val}", "WARNING")
                else:
                    saved_state.append({
                        'op': op_ref,
                        'family': 'COMP',
                        'externaltox': tox_val,
                        'externaltox_readonly': op_ref.par.externaltox.readOnly,
                        'enableexternaltox': enable_val,
                    })

        # Phase 1b: Collect Embody tags to strip from all descendants
        # (including the target itself). Recipients don't need Embody
        # metadata -- it would cause confusion if they have Embody installed.
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
    def _parFingerprint(operator) -> tuple:
        """Fingerprint an operator's non-default parameters.

        Mirrors what a TDN export serializes (non-default pars only), so a
        parameter edit -- constant value, expression, or bind -- changes the
        fingerprint and marks the TDN COMP dirty. Captures the AUTHORED value
        (expr for expression mode, bindExpr for bind, val for constant), never
        .eval(), so no cook side effects and a match for what TDN records.
        Embody-managed About-page metadata (Build/Date/Touchbuild) is excluded
        to match TDN export and avoid spurious dirty flags on build bumps.
        """
        skip = {'Build', 'Date', 'Touchbuild'}
        out = []
        for p in operator.pars():
            try:
                if p.name in skip or p.isDefault:
                    continue
                mode = p.mode.name
                if mode == 'EXPRESSION':
                    v = p.expr
                elif mode == 'BIND':
                    v = p.bindExpr
                else:
                    v = p.val
                out.append((p.name, mode, str(v)))
            except Exception:
                # A single unreadable par must not break dirty detection.
                continue
        out.sort()
        return tuple(out)

    @staticmethod
    def _computeTDNFingerprint(comp, tdn_paths: set = None,
                               exclude_tag: str = None) -> tuple:
        """Compute a hashable fingerprint of a TDN COMP's network structure.

        Used instead of oper.dirty for TDN COMPs (which always reads True
        because externaltox is empty). Captures everything a TDN export
        records: the root COMP's own non-default parameters, plus each
        embedded operator's name, type, position, size, color, tags, flags,
        comment, non-default parameters, connections, and annotations.

        Recurses into child COMPs that are NOT separately TDN-externalized,
        so changes deep inside nested COMPs (e.g. editing a POP inside a
        geometryCOMP) are detected by the parent's fingerprint. A separately
        TDN-externalized child is recorded only structurally -- its own
        parameters are tracked by its own fingerprint, mirroring how a TDN
        export emits a reference rather than the child's content.
        """
        parts = []
        # The root COMP's own parameters are part of its TDN export, so a
        # top-level parameter edit must change the fingerprint. (Without this,
        # only structural/layout changes were detected -- param edits on a TDN
        # COMP went unnoticed by dirty detection.)
        parts.append(('__self_pars__', EmbodyExt._parFingerprint(comp)))
        for c in sorted(comp.children, key=lambda c: c.name):
            # Skip annotations -- they're fingerprinted separately below
            if c.type == 'annotate':
                continue
            # Excluded COMPs are omitted from the export, so omit them from
            # the fingerprint too -- otherwise an app-managed excluded child
            # (e.g. a runtime-materialized copy) would dirty its parent on
            # every change the app makes to it.
            if exclude_tag and c.isCOMP and exclude_tag in c.tags:
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
            # A separately TDN-externalized child COMP is referenced, not
            # embedded -- its params/content are tracked by its own
            # fingerprint. Embedded ops (non-COMP children, or COMPs without
            # their own .tdn) have their params recorded here.
            is_embedded_comp = c.isCOMP and (tdn_paths is None or c.path not in tdn_paths)
            if not c.isCOMP or is_embedded_comp:
                parts.append((c.name, 'pars', EmbodyExt._parFingerprint(c)))
            if is_embedded_comp:
                # Honor exclusion ONLY at the boundary's direct children
                # (this top-level call). Nested excluded COMPs are serialized
                # as normal content by the export, so the fingerprint must
                # track them too (pass exclude_tag=None into the recursion)
                # -- otherwise an app edit to a nested "excluded" COMP would
                # go undetected and the .tdn would drift stale.
                child_fp = EmbodyExt._computeTDNFingerprint(
                    c, tdn_paths, None)
                parts.append((c.name, 'children', child_fp))
        # All annotations (utility=True or False) -- uses annotation-specific attrs
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

    def _getTDNPaths(self) -> set:
        """Return the set of all TDN-externalized COMP paths."""
        return {path for path, _ in self._getTDNStrategyComps()}

    def _isTDNDirty(self, comp, tdn_paths: set = None,
                    exclude_tag: str = None) -> bool:
        """Check if a TDN COMP's network has changed since last export.

        Callers sweeping many COMPs in one pass (Update/dirtyHandler) should
        precompute tdn_paths + exclude_tag once and pass them in, so the
        per-COMP full-table scan in _getTDNPaths() and the par.eval() of the
        exclude tag don't repeat for every COMP on every Refresh.
        """
        if tdn_paths is None:
            tdn_paths = self._getTDNPaths()
        if exclude_tag is None:
            exclude_tag = self.my.par.Tdnexcludetag.eval()
        current = self._computeTDNFingerprint(comp, tdn_paths, exclude_tag)
        stored = self._tdn_fingerprints.get(comp.path)
        if stored is None:
            # No stored fingerprint -- assume clean (just initialized)
            self._tdn_fingerprints[comp.path] = current
            return False
        return current != stored

    def _storeTDNFingerprint(self, comp, tdn_paths: set = None,
                             exclude_tag: str = None) -> None:
        """Snapshot the TDN COMP's network structure after export."""
        if tdn_paths is None:
            tdn_paths = self._getTDNPaths()
        if exclude_tag is None:
            exclude_tag = self.my.par.Tdnexcludetag.eval()
        self._tdn_fingerprints[comp.path] = self._computeTDNFingerprint(
            comp, tdn_paths, exclude_tag)

    def _getStrategyFilePath(self, op_path: str, strategy: str) -> Optional[str]:
        """Return the rel_file_path for a given operator + strategy, or None."""
        table = self.Externalizations
        if not table:
            return None
        has_strategy_col = table[0, 'strategy'] is not None
        for i in range(1, table.numRows):
            if self._cellVal(i, 'path') == op_path:
                if has_strategy_col and self._cellVal(i, 'strategy') == strategy:
                    return self._cellVal(i, 'rel_file_path')
                elif not has_strategy_col:
                    return self._cellVal(i, 'rel_file_path')
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
            if self._cellVal(i, 'strategy') != 'tdn':
                continue
            path = self._cellVal(i, 'path')
            if path == exclude_path:
                continue
            rel = self._cellVal(i, 'rel_file_path')
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
            if self._cellVal(i, 'path') == comp.path:
                s = self._cellVal(i, 'strategy')
                if s in ('tox', 'tdn'):
                    return s
        return None

    def SaveCurrentComp(self) -> None:
        """Update only the COMP we're currently working inside of (Ctrl/Cmd+Alt+U)."""
        if self._performMode:
            return
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
            if self._cellVal(i, 'path') == comp_path:
                if has_strategy_col:
                    s = self._cellVal(i, 'strategy')
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
                # Preserve 'Par' dirty state when oper.dirty is False --
                # parameter changes are tracked independently from TD's
                # native dirty flag and should only be cleared on Save.
                if dirty or self._cellVal(oper.path, 'dirty') != 'Par':
                    self.Externalizations[oper.path, 'dirty'] = dirty
            except Exception as e:
                self.Log(f"Failed to update dirty state for {oper.path}: {e}", "DEBUG")
            if dirty and update:
                self.Save(oper.path)
                updates.append(oper.path)

        # TDN-strategy COMPs -- use network fingerprint instead of oper.dirty
        # (oper.dirty is always True when externaltox is empty). This is the
        # SINGLE place TDN dirty state is evaluated per sweep: the fingerprint
        # already covers both structural AND authored-parameter changes, so
        # there is no separate compareParameters() pass for TDN COMPs (that
        # was redundant work and, reading .eval(), the source of false-dirty
        # churn). Precompute tdn_paths + exclude_tag once and reuse them for
        # every COMP so the per-COMP full-table scan doesn't repeat.
        if self._tdnEnabled():
            tdn_paths = self._getTDNPaths()
            exclude_tag = self.my.par.Tdnexcludetag.eval()
            for oper in self.getExternalizedOps(COMP, strategy='tdn'):
                # Skip root "/" (Full Project export, not a managed COMP) and
                # excluded app-managed COMPs -- never auto dirty-check/save.
                if oper.path == '/' or exclude_tag in oper.tags:
                    continue
                dirty = self._isTDNDirty(oper, tdn_paths, exclude_tag)
                try:
                    if dirty:
                        self.Externalizations[oper.path, 'dirty'] = 'True'
                    elif self._cellVal(oper.path, 'dirty'):
                        # Clean now -- clear any stale dirty flag left by a
                        # prior scan (e.g. an edit that was reverted). Without
                        # this the indicator sticks on 'True'/'Par' until a
                        # real SaveTDN runs.
                        self.Externalizations[oper.path, 'dirty'] = ''
                except Exception as e:
                    self.Log(f"Failed to update dirty state for {oper.path}: {e}", "DEBUG")
                if dirty and update:
                    self.SaveTDN(oper.path)
                    updates.append(oper.path)

        return updates

    def updateDirtyStates(self, externalizationsFolder: str) -> None:
        """Update dirty states and check for path/parameter changes."""
        dirties = self.dirtyHandler(False)
        param_changes = []

        for oper in self.getExternalizedOps(COMP) + self.getExternalizedOps(DAT):
            # TDN-strategy COMPs don't use externaltox -- their rel_file_path
            # is managed by _handleTDNAddition / _addToTable, not the par.
            # Their dirty state (structural AND parameter) was already fully
            # evaluated by dirtyHandler(False) above via the network
            # fingerprint, so there is no separate compareParameters() pass
            # here. Skip them to avoid overwriting the .tdn path with "".
            if oper.family == 'COMP' and self._getCompStrategy(oper) == 'tdn':
                continue

            current_path = self.getExternalPath(oper)
            try:
                table_path = self.normalizePath(self._cellVal(oper.path, 'rel_file_path'))
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

        # Export TDN -- protect .tdn files belonging to OTHER tracked
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
            # Prime the dirty-detection baseline now, on the just-exported
            # (clean) network, so the dirty indicator is correct immediately
            # instead of being set lazily by the first _isTDNDirty scan. Without
            # this, a param edit landing before that first scan would be absorbed
            # into the baseline and the COMP would wrongly read clean. Mirrors
            # SaveTDN, which snapshots the fingerprint after every export.
            self._storeTDNFingerprint(oper)
            self.Log(f"Added TDN '{oper.path}'", "SUCCESS")

            # Cascade: auto-tag child COMPs if enabled
            if self.my.par.Tdncascade.eval():
                self._cascadeTDNTag(oper)
        else:
            self.Log(f"TDN export failed for {oper.path}: {result.get('error')}", "ERROR")

    def _cascadeTDNTag(self, parent_comp: OP) -> None:
        """Auto-tag direct child COMPs for TDN externalization.

        Uses depth=1 (direct children only). Recursion happens naturally
        through the applyTagToOperator -> _handleTDNAddition ->
        _cascadeTDNTag chain, processing each level in order.
        """
        tdn_tag = self.my.par.Tdntag.val
        for child in parent_comp.findChildren(type=COMP, depth=1):
            # Exclude tag wins over cascade auto-tagging: never automatically
            # mark an excluded COMP for TDN. (Explicit user tagging still works.)
            if self.my.ext.TDN._hasExcludeTag(child):
                continue
            if tdn_tag not in child.tags:
                self.applyTagToOperator(child, tdn_tag)

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
                if self._cellVal(row, 'path') == oper.path:
                    try:
                        current_build = int(self._cellVal(row, 'build'))
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
            if self._cellVal(row, 'path') == oper.path:
                if has_strategy_col:
                    row_strategy = self._cellVal(row, 'strategy')
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

    def _reconstructAboutPage(self, comp: 'COMP', comp_path: str) -> None:
        """Reconstruct Embody's About custom page from externalizations.tsv.

        Called during TDN reconstruction so About pages appear in TD even
        though they are no longer serialized into .tdn files.
        """
        build_cell = self.Externalizations[comp_path, 'build']
        if build_cell is None:
            return
        try:
            build_num = int(build_cell.val) if hasattr(build_cell, 'val') else int(build_cell)
        except (ValueError, TypeError):
            build_num = 1

        touch_cell = self.Externalizations[comp_path, 'touch_build']
        touch_build = (touch_cell.val if hasattr(touch_cell, 'val') else str(touch_cell)) if touch_cell else str(app.build)

        ts_cell = self.Externalizations[comp_path, 'timestamp']
        date_str = (ts_cell.val if hasattr(ts_cell, 'val') else str(ts_cell)) if ts_cell else ''

        build_page = next((p for p in comp.customPages if p.name == 'About'), None)
        if not build_page:
            build_page = comp.appendCustomPage('About')

        self.setupBuildParameters(comp, build_page, build_num, touch_build)
        # Override Date with TSV timestamp (not current time from setupBuildParameters)
        if hasattr(comp.par, 'Date'):
            comp.par.Date.val = date_str

    # ==========================================================================
    # CONTINUITY & RENAME HANDLING
    # ==========================================================================

    def checkOpsForContinuity(self, externalizationsFolder: str) -> None:
        """Check for renamed, moved, or missing operators and update accordingly."""
        self._checkExternalToxPar()

        try:
            rows_to_check = []
            tdn_comp_paths = set()
            headers = [self._cellVal(0, c)
                       for c in range(self.Externalizations.numCols)]
            has_strategy = 'strategy' in headers
            for i in range(1, self.Externalizations.numRows):
                row_path = self._cellVal(i, 'path')
                if row_path:
                    rel_file_path = self.normalizePath(self._cellVal(i, 'rel_file_path'))
                    row_type = self._cellVal(i, 'type')
                    strategy = self._cellVal(i, 'strategy') if has_strategy else ''
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
            # simultaneously -- handle as a single batch operation.
            ancestor_result = self._detectAncestorRename(rows_to_check)
            if ancestor_result:
                old_prefix, new_prefix = ancestor_result
                success = self._handleAncestorRename(
                    old_prefix, new_prefix, rows_to_check,
                    externalizationsFolder)
                if success:
                    return
                self.Log("Ancestor rename batch failed, falling back to "
                         "per-operator handling", "WARNING")

            processed_ops = set()
            missing_with_files = []

            for old_op_path, rel_file_path, row_type, strategy in rows_to_check:
                if old_op_path in processed_ops:
                    continue

                # TDN-strategy COMPs don't set externaltox/file -- just verify the op exists
                is_tdn = (strategy == 'tdn') if has_strategy else (row_type == 'tdn')
                if is_tdn:
                    if not op(old_op_path):
                        # Try rename detection first -- a TDN-tagged COMP in
                        # the same parent that isn't tracked is likely a rename.
                        found = self._findMovedTDNOp(
                            old_op_path, rel_file_path, processed_ops)
                        if not found:
                            # Check if .tdn file exists on disk
                            if rel_file_path:
                                abs_tdn = self.buildAbsolutePath(
                                    self.normalizePath(rel_file_path))
                                if abs_tdn.is_file():
                                    missing_with_files.append(
                                        (old_op_path, rel_file_path, 'tdn'))
                                    continue
                            self.Log(f"Operator for TDN entry '{old_op_path}' no longer exists", "WARNING")
                            self._removeTDNStrategy(old_op_path)
                    continue

                # Skip operators inside TDN-strategy COMPs when appropriate:
                # - Always skip if no individual strategy (purely TDN-managed)
                # - Skip individually-externalized children only if the parent
                #   TDN COMP is completely missing (crash recovery before
                #   reconstruction). If the parent exists but is empty, the
                #   child was genuinely deleted -- check it normally.
                #   (Save-cycle stripping is protected by suppress_refresh.)
                parent_tdn = next(
                    (p for p in tdn_comp_paths
                     if old_op_path.startswith(p + '/')), None)
                if parent_tdn is not None:
                    if not strategy:
                        continue
                    if parent_tdn in stripped_tdn_paths and not op(parent_tdn):
                        continue

                existing_op = op(old_op_path)

                if existing_op:
                    # Verify this is actually the SAME operator (not a different one at same path)
                    # by checking if externaltox matches what we expect
                    current_ext_path = self.getExternalPath(existing_op)

                    if current_ext_path == rel_file_path:
                        # Same operator, still mapped to the same file -- no action.
                        # Previously called _updateOpTimestamp here, which bumped the
                        # TSV timestamp to the externalized file's mtime. That caused
                        # per-save churn: the save's strip/restore cycle re-writes
                        # every .tdn file (bumping every mtime), and continuity then
                        # propagated those bumps into every TSV row even when content
                        # was unchanged. The timestamp now reflects only explicit
                        # Save/SaveTDN/rename events.
                        pass
                    else:
                        # Different operator at this path! The original was likely moved.
                        # Search for the moved operator
                        found_moved = self._findMovedOp(
                            old_op_path, rel_file_path, externalizationsFolder, processed_ops
                        )
                        if not found_moved:
                            # Check if file exists on disk -- defer to user preference
                            if rel_file_path:
                                abs_file = self.buildAbsolutePath(
                                    self.normalizePath(rel_file_path))
                                if abs_file.is_file():
                                    missing_with_files.append(
                                        (old_op_path, rel_file_path, 'replaced'))
                                    continue
                            # Operator was replaced, not moved - remove old entry
                            self.Log(f"Operator at '{old_op_path}' was replaced", "WARNING")
                            self._handleMissingOperator(old_op_path, rel_file_path)
                else:
                    # Operator no longer exists at path - check for rename/move
                    found_renamed = self._findMovedOp(
                        old_op_path, rel_file_path, externalizationsFolder, processed_ops
                    )
                    if not found_renamed:
                        # Check if file exists on disk -- defer to user preference
                        if rel_file_path:
                            normalized = self.normalizePath(rel_file_path)
                            abs_file = self.buildAbsolutePath(normalized)
                            self.Debug(f"File check: rel='{rel_file_path}' norm='{normalized}' abs='{abs_file}' exists={abs_file.is_file()}")
                            if abs_file.is_file():
                                missing_with_files.append(
                                    (old_op_path, rel_file_path, 'missing'))
                                continue
                        else:
                            self.Debug(f"No rel_file_path for '{old_op_path}'")
                        self._handleMissingOperator(old_op_path, rel_file_path)

            # Handle operators whose files still exist on disk -- prompt user
            if missing_with_files:
                self._handleMissingOpsWithFiles(missing_with_files)

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

        if not comps_with_filefolder:
            return

        embody_path = self.my.path
        internal, external = [], []
        for comp in comps_with_filefolder:
            if comp.path == embody_path or comp.path.startswith(embody_path + '/'):
                internal.append(comp)
            else:
                external.append(comp)

        def _reset(comp):
            try:
                comp.par.externaltox.expr = ''
                comp.par.externaltox = ''
                self.Log(f"Reset externaltox for '{comp.path}'", "SUCCESS")
            except Exception as e:
                self.Log(f"Error resetting '{comp.path}'", "ERROR", str(e))

        for comp in internal:
            _reset(comp)

        if external:
            message = "Found COMPs using deprecated 'me.parent().fileFolder':\n\n"
            message += "\n".join([f"- {comp.path}" for comp in external])
            message += "\n\nReset these paths?"
            if ui.messageBox('Embody', message, buttons=['No', 'Yes']) == 1:
                for comp in external:
                    _reset(comp)

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
            if self._cellVal(i, 'strategy') == 'tdn':
                p = self._cellVal(i, 'path')
                if p != old_op_path:
                    tracked_tdn_paths.add(p)

        # Embody exclusion -- same as _getTDNStrategyComps
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
                    f"Multiple untracked TDN COMPs in {old_parent} -- "
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
                # Old file missing -- re-export instead
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
            child_path = self._cellVal(i, 'path')
            if child_path.startswith(old_prefix_slash):
                children.append((
                    child_path,
                    self._cellVal(i, 'rel_file_path'),
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
                # Child no longer exists at expected new path -- remove stale row
                self._handleMissingOperator(child_path, child_rel_file)

    def _detectAncestorRename(self, rows_to_check):
        """Detect if multiple missing operators share a common path prefix change.

        When a COMP that is an ancestor of many externalized operators is renamed
        (e.g., /embody -> /myproject), all tracked operators under it go missing
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

        self.Log(f"Detected ancestor rename: {ancestor_path} -> {new_prefix} "
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

        # Include ExternalizationsFolder prefix for disk path operations
        if externalizationsFolder:
            old_disk_segment = externalizationsFolder + '/' + old_dir_segment
            new_disk_segment = externalizationsFolder + '/' + new_dir_segment
        else:
            old_disk_segment = old_dir_segment
            new_disk_segment = new_dir_segment

        # --- Phase A: Calculate what will change ---
        affected = []
        for old_path, rel_file, row_type, strategy in rows_to_check:
            if old_path.startswith(old_prefix + '/') or old_path == old_prefix:
                new_path = new_prefix + old_path[len(old_prefix):]
                if rel_file.startswith(old_disk_segment + '/'):
                    new_rel_file = new_disk_segment + rel_file[len(old_disk_segment):]
                elif rel_file == old_disk_segment:
                    new_rel_file = new_disk_segment
                else:
                    new_rel_file = rel_file
                affected.append((old_path, new_path, rel_file, new_rel_file,
                                row_type, strategy))

        if not affected:
            return False

        # --- Phase B: Prompt user ---
        msg = (f"Detected rename: {old_prefix} -> {new_prefix}\n\n"
               f"{len(affected)} externalized files will be moved:\n"
               f"  {old_disk_segment}/...  ->  {new_disk_segment}/...\n\n"
               f"This will rename the folder on disk and update all tracking.\n"
               f"Cancel to leave files at their current location.")
        choice = self._messageBox('Embody -- Ancestor Rename Detected', msg,
                                  ['Cancel', 'Proceed'])
        if choice != 1:
            self.Log(f"Ancestor rename cancelled by user: "
                     f"{old_prefix} -> {new_prefix}", "INFO")
            return False

        # --- Phase C: Rename directory on disk ---
        project_folder = Path(project.folder)
        old_dir = project_folder / old_disk_segment
        new_dir = project_folder / new_disk_segment

        if not old_dir.exists():
            self.Log(f"Source directory not found: {old_dir}", "ERROR")
            self._messageBox('Embody Error',
                             f'Source directory not found:\n{old_disk_segment}/',
                             ['OK'])
            return False

        if new_dir.exists():
            self.Log(f"Target directory already exists: {new_dir}", "ERROR")
            self._messageBox('Embody Error',
                             f'Cannot rename: directory "{new_disk_segment}/" '
                             f'already exists.',
                             ['OK'])
            return False

        try:
            old_dir.rename(new_dir)
            self.Log(f"Renamed directory: {old_disk_segment}/ -> "
                     f"{new_disk_segment}/", "SUCCESS")
        except Exception as e:
            self.Log("Failed to rename directory", "ERROR", str(e))
            self._messageBox('Embody Error',
                             f'Failed to rename directory:\n{e}',
                             ['OK'])
            return False

        # --- Phase D: Update externalizations table ---
        table = self.Externalizations
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        for old_path, new_path, old_rel, new_rel, _, _ in affected:
            for i in range(1, table.numRows):
                if self._cellVal(i, 'path') == old_path:
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

        self.Log(f"Ancestor rename complete: {old_prefix} -> {new_prefix} "
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

        return True

    def _handleMissingOperator(self, old_op_path, old_rel_file_path, delete_file=True):
        """Handle an operator that no longer exists."""
        self.cleanupDuplicateRows(old_op_path)

        # Truly missing - remove the specific row from the table
        self.Log(f"Operator '{old_op_path}' no longer exists!", "WARNING")
        normalized = self.normalizePath(old_rel_file_path)
        for i in range(1, self.Externalizations.numRows):
            if (self._cellVal(i, 'path') == old_op_path
                    and self.normalizePath(self._cellVal(i, 'rel_file_path')) == normalized):
                self.RemoveListerRow(old_op_path, old_rel_file_path,
                                     delete_file=delete_file)
                break

    def _handleMissingOpsWithFiles(self, missing_ops: list) -> None:
        """Handle operators removed from the network whose files still exist.

        Prompts the user (or applies their saved preference) to decide whether
        to keep or delete the external files when removing the table entries.

        Args:
            missing_ops: List of (op_path, rel_file_path, reason) tuples where
                reason is 'tdn', 'replaced', or 'missing'.
        """
        # Suppress dialog during test runs to prevent modal spam --
        # rapid operator create/destroy cycles can trigger continuity
        # checks that find transient sandbox ops as "missing".
        # Two-layer protection: runtime flag AND path-based filtering.
        try:
            runner = getattr(op, 'unit_tests', None)
            if runner:
                # Layer 1: If test runner is actively running, suppress entirely
                runner_ext = getattr(
                    getattr(runner, 'ext', None), 'TestRunnerExt', None)
                if runner_ext and getattr(runner_ext, '_running', False):
                    self.Debug(
                        f'Continuity dialog suppressed: test runner active '
                        f'({len(missing_ops)} missing ops)')
                    return
                # Layer 2: Filter out sandbox paths even when runner isn't
                # active (handles reinit, between-suite gaps, post-failure).
                # Covers both the standard sandbox COMP and root-level
                # test sandboxes (e.g. /_test_dat_restore).
                sandbox_comp = getattr(runner, 'op', lambda x: None)(
                    'test_sandbox')
                sandbox_prefixes = []
                if sandbox_comp:
                    sandbox_prefixes.append(sandbox_comp.path + '/')
                sandbox_prefixes.append('/_test_')
                filtered = [(p, f, r) for p, f, r in missing_ops
                            if not any(p.startswith(px)
                                       for px in sandbox_prefixes)]
                if len(filtered) < len(missing_ops):
                    self.Debug(
                        f'Filtered {len(missing_ops) - len(filtered)} '
                        f'test sandbox ops from continuity check')
                    missing_ops = filtered
                    if not missing_ops:
                        return
        except Exception:
            pass

        filecleanup_par = getattr(self.my.par, 'Filecleanup', None)
        preference = filecleanup_par.eval() if filecleanup_par else 'ask'

        if preference == 'ask':
            op_list = '\n'.join(f'  - {path}' for path, _, _ in missing_ops)
            count = len(missing_ops)
            noun = 'operator' if count == 1 else 'operators'
            s = '' if count == 1 else 's'
            msg = (f'{count} externalized {noun} removed from the network:\n\n'
                   f'{op_list}\n\n'
                   f'External file{s} still exist{"s" if count == 1 else ""} on disk.\n'
                   f'Remove from tracking only, or also delete file{s}?')

            title = ('Removed Operator Detected' if count == 1
                     else 'Removed Operators Detected')
            choice = ui.messageBox(
                title,
                msg,
                buttons=[f'Keep File{s}', f'Delete File{s}',
                         'Always Keep', 'Always Delete'])

            # ui.messageBox returns 0-based button index (0 also for dialog close)
            if choice == 0:
                delete_files = False  # Keep File (or dialog closed)
            elif choice == 1:
                delete_files = True   # Delete File
            elif choice == 2:
                delete_files = False  # Always Keep
                if filecleanup_par:
                    self.my.par.Filecleanup = 'keep'
                self.Log('File cleanup preference set to Always Keep', 'INFO')
            elif choice == 3:
                delete_files = True   # Always Delete
                if filecleanup_par:
                    self.my.par.Filecleanup = 'delete'
                self.Log('File cleanup preference set to Always Delete', 'INFO')
            else:
                return
        elif preference == 'keep':
            delete_files = False
        else:  # 'delete'
            delete_files = True

        for op_path, rel_file_path, reason in missing_ops:
            if reason == 'tdn':
                self.Log(f"Operator for TDN entry '{op_path}' no longer exists",
                         'WARNING')
                self._removeTDNStrategy(op_path, delete_file=delete_files)
            else:
                if reason == 'replaced':
                    self.Log(f"Operator at '{op_path}' was replaced", 'WARNING')
                self._handleMissingOperator(
                    op_path, rel_file_path,
                    delete_file=delete_files)

    def updateMovedOp(self, new_op: OP, old_op_path: str, old_rel_file_path: str, externalizationsFolder: str) -> None:
        """Update table and files when an operator is renamed."""
        try:
            # Cleanup duplicates
            for i in range(1, self.Externalizations.numRows):
                if self._cellVal(i, 'path') == new_op.path:
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
            path = self._cellVal(i, 'path')
            if path:
                paths.add(path)
        for path in paths:
            self.cleanupDuplicateRows(path)

    def cleanupDuplicateRows(self, path: str) -> Optional[int]:
        """Remove duplicate rows for a path, keeping most recent per type.

        A COMP can legitimately have both a TOX row and a TDN row -- these are
        different externalization types, not duplicates. Only rows with the
        same path AND same type are true duplicates.
        """
        type_groups = {}

        for i in range(1, self.Externalizations.numRows):
            if self._cellVal(i, 'path') == path:
                row_type = self._cellVal(i, 'type')
                if row_type not in type_groups:
                    type_groups[row_type] = {'indices': [], 'timestamps': []}
                type_groups[row_type]['indices'].append(i)
                try:
                    ts_str = self._cellVal(i, 'timestamp')
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
            row_type = self._cellVal(i, 'type')
            self.Externalizations.deleteRow(i)
            self.Log(f"Removed duplicate row {i} for {path} (type={row_type})", "INFO")

        return kept_row

    def _buildPathGroups(self) -> dict:
        """Map normalized external paths to lists of operators sharing them.

        Only includes operators with Embody tags that are not inside
        TD clone hierarchies or replicator outputs.
        """
        embody_tags = self.getTags()
        path_groups = {}

        for oper in self.root.findChildren(type=COMP, parName='externaltox'):
            if not any(tag in oper.tags for tag in embody_tags):
                continue
            if self.isInsideClone(oper) or self.isReplicant(oper):
                continue
            path = self.normalizePath(oper.par.externaltox.eval())
            if path:
                path_groups.setdefault(path, []).append(oper)

        for oper in self.root.findChildren(type=DAT, parName='file'):
            if not any(tag in oper.tags for tag in embody_tags):
                continue
            if self.isInsideClone(oper) or self.isReplicant(oper):
                continue
            path = self.normalizePath(oper.par.file.eval())
            if path:
                path_groups.setdefault(path, []).append(oper)

        return path_groups

    def checkForDuplicates(self) -> None:
        """Check for and handle duplicate external file paths.

        Groups all operators sharing the same external path, then:
        - For replicants: auto-tags all replicants (master is the template)
        - For COMPs with TD clone relationships: auto-tags clones
        - For DATs inside cloned COMPs: auto-tags DATs in clone COMPs
        - For others: collects unresolved groups. When 2+ groups
          remain, offers a single batch prompt (auto-resolve all /
          review individually / skip); a single group goes straight
          to the per-group prompt.
        """
        unresolved = []
        for path, ops in self._buildPathGroups().items():
            if len(ops) < 2:
                continue
            if any('clone' in o.tags for o in ops):
                continue
            if self._resolveReplicants(ops):
                continue
            if self._resolveClonesByCloningAPI(ops):
                continue
            if self._resolveDATsInClonedCOMPs(ops):
                continue
            if self._resolveByTemplateMarker(ops):
                continue
            unresolved.append((path, ops))

        if not unresolved:
            return

        if len(unresolved) == 1:
            path, ops = unresolved[0]
            self._promptForDuplicateGroup(path, ops)
            return

        choice = self._promptForBatchResolution(unresolved)
        if choice == 'dismiss':
            return
        if choice == 'auto':
            for path, ops in unresolved:
                self._autoResolveFirstAsMaster(path, ops)
            return
        for path, ops in unresolved:
            self._promptForDuplicateGroup(path, ops)

    def _resolveClonesByCloningAPI(self, ops: list) -> bool:
        """Try to resolve master/clone using TD's native clone API.

        Returns True if resolution succeeded (all clones tagged),
        False if the API doesn't apply (DATs, or COMPs without
        clone relationships).
        """
        if not all(o.family == 'COMP' for o in ops):
            return False

        master = None
        ops_set = set(ops)

        # Check .clones property -- master is the op whose clones overlap
        for o in ops:
            try:
                clones = o.clones
                if clones and ops_set.intersection(clones):
                    master = o
                    break
            except Exception:
                pass

        # Fallback: check par.clone -- it points FROM clone TO master
        if not master:
            for o in ops:
                clone_ref = o.par.clone.eval()
                if clone_ref and clone_ref in ops_set and clone_ref is not o:
                    master = clone_ref
                    break

        if not master:
            return False

        for o in ops:
            if o is not master:
                self._handleDuplicateAsReference(o)

        self.Log(
            f"Auto-resolved clone master '{master.path}' for path "
            f"shared by {len(ops)} operators", "SUCCESS")
        return True

    def _resolveDATsInClonedCOMPs(self, ops: list) -> bool:
        """Auto-resolve DATs inside cloned COMPs.

        When DATs share an external path and their ancestor COMPs are in
        a clone relationship, auto-tag DATs inside clone COMPs.

        Returns True if resolution succeeded, False if not applicable.
        """
        if not all(o.family == 'DAT' for o in ops):
            return False

        masters = []
        clones = []
        for dat in ops:
            if self.isInsideClone(dat):
                clones.append(dat)
            else:
                masters.append(dat)

        if not masters or not clones:
            return False

        for dat in clones:
            self._handleDuplicateAsReference(dat)

        self.Log(
            f"Auto-resolved {len(clones)} DAT{'s' if len(clones) > 1 else ''} "
            f"inside cloned COMPs (master: "
            f"{', '.join(d.path for d in masters)})", "SUCCESS")
        return True

    def _resolveReplicants(self, ops: list) -> bool:
        """Auto-resolve replicant groups without prompting.

        If any op in the group is a replicant (has a replicator ancestor),
        tag all replicants as clones. The non-replicant op (if any) is
        treated as master.

        Returns True if any replicants were found and tagged.
        """
        replicants = [o for o in ops if self.isReplicant(o)]
        if not replicants:
            return False

        for o in replicants:
            self._handleDuplicateAsReference(o)

        non_replicants = len(ops) - len(replicants)
        self.Log(
            f"Auto-tagged {len(replicants)} replicant{'s' if len(replicants) != 1 else ''} "
            f"as clones ({non_replicants} master{'s' if non_replicants != 1 else ''} retained)",
            "SUCCESS")
        return True

    def _resolveByTemplateMarker(self, ops: list) -> bool:
        """Auto-resolve a duplicate group using the master-name convention.

        Reads the ``Templatemaster`` parameter (default ``__template__``).
        If exactly one operator in the group has that name as a path
        component (e.g. a ``__template__`` parent COMP), it is tagged as
        the master and the rest as clones -- no prompt. This makes the
        common app-generated-instances pattern (one template + many
        copies) resolve silently, while staying invisible to projects
        that don't use the convention.

        Returns True only when the marker matches exactly one operator.
        An empty parameter disables the behavior; 0 or 2+ matches fall
        through to the normal prompt so the choice stays unambiguous.
        """
        marker = self.my.par.Templatemaster.eval().strip()
        if not marker:
            return False

        matches = [o for o in ops if marker in o.path.strip('/').split('/')]
        if len(matches) != 1:
            return False

        master = matches[0]
        for o in ops:
            if o is not master:
                self._handleDuplicateAsReference(o)
        clones = len(ops) - 1
        self.Log(
            f"Auto-resolved '{master.path}' as master via name convention "
            f"'{marker}' ({clones} clone{'s' if clones != 1 else ''})",
            "SUCCESS")
        return True

    def _duplicateButtonLabels(self, ops: list) -> list:
        """Build short, distinguishable button labels for a duplicate group.

        Operators in a duplicate group share an external path and usually
        a name, so the op name alone is ambiguous (every button reads the
        same). Label each by the first path segment that differs across
        the group, prefixed with its list number so it maps 1:1 to the
        numbered list in the dialog body.
        """
        seg_lists = [o.path.strip('/').split('/') for o in ops]
        min_len = min(len(s) for s in seg_lists)
        diff_idx = next(
            (idx for idx in range(min_len)
             if len({s[idx] for s in seg_lists}) > 1),
            None)
        labels = []
        for i, segs in enumerate(seg_lists):
            seg = segs[diff_idx] if diff_idx is not None else ops[i].name
            labels.append(f"{i+1}: {seg}")
        return labels

    def _promptForDuplicateGroup(self, path: str, ops: list) -> None:
        """Show a single dialog for a group of operators sharing the same path.

        The user picks which operator is the master; all others get
        clone tags. Dismiss skips without tagging (will re-prompt on
        next cycle). Groups larger than ``_MAX_MANUAL_BUTTONS`` are
        routed to a strategy prompt, since a button per operator becomes
        unreadable and overflows the dialog.
        """
        op_list = '\n'.join(
            f"  {i+1}. {o.path} ({o.family})" for i, o in enumerate(ops))

        if len(ops) > self._MAX_MANUAL_BUTTONS:
            self._promptForLargeDuplicateGroup(path, ops, op_list)
            return

        buttons = ['Dismiss'] + self._duplicateButtonLabels(ops)

        choice = self._messageBox(
            'Duplicate Path Detected',
            f"Multiple operators share the external path:\n"
            f"  {path}\n\n"
            f"Operators:\n{op_list}\n\n"
            f"Select the MASTER (others will be tagged as clones).\n"
            f"'Dismiss' to skip for now.",
            buttons=buttons)

        if choice == 0:
            return

        master_idx = choice - 1
        if 0 <= master_idx < len(ops):
            for i, o in enumerate(ops):
                if i != master_idx:
                    self._handleDuplicateAsReference(o)
            self.Log(
                f"User selected '{ops[master_idx].path}' as master "
                f"for '{path}'", "SUCCESS")

    def _promptForLargeDuplicateGroup(
            self, path: str, ops: list, op_list: str) -> None:
        """Prompt for a duplicate group too large for a per-op button row.

        A button per operator is unusable past a handful, so offer a
        strategy choice instead: skip, or keep the first-listed operator
        as master. Points the user at the ``Templatemaster`` naming
        convention for hands-off resolution next time.
        """
        marker = self.my.par.Templatemaster.eval().strip()
        if marker:
            tip = (f"Tip: name one operator's COMP '{marker}' to auto-resolve "
                   f"groups like this without prompting.")
        else:
            tip = ("Tip: set the 'Template Master Name' parameter to "
                   "auto-resolve groups like this by naming convention.")

        choice = self._messageBox(
            'Duplicate Path Detected',
            f"{len(ops)} operators share the external path:\n"
            f"  {path}\n\n"
            f"Operators:\n{op_list}\n\n"
            f"That's too many to choose from individually.\n"
            f"  * Keep first as master: tag operator 1 as master, "
            f"rest as clones.\n"
            f"  * Dismiss: skip for now (re-prompts next cycle).\n\n"
            f"{tip}",
            buttons=['Dismiss', 'Keep first as master'])

        if choice == 1:
            self._autoResolveFirstAsMaster(path, ops)

    def _promptForBatchResolution(self, unresolved: list) -> str:
        """Ask how to handle multiple unresolved duplicate groups.

        Returns 'dismiss', 'review', or 'auto'.
        """
        n = len(unresolved)
        preview_limit = 5
        preview_lines = [f"  - {path}" for path, _ in unresolved[:preview_limit]]
        if n > preview_limit:
            preview_lines.append(f"  ... and {n - preview_limit} more")
        preview = '\n'.join(preview_lines)

        choice = self._messageBox(
            'Duplicate Paths Detected',
            f"{n} groups of operators share external file paths:\n\n"
            f"{preview}\n\n"
            f"How would you like to resolve them?\n\n"
            f"  * Auto-resolve all: in each group, keep the first\n"
            f"    listed operator as master; tag the rest as clones.\n"
            f"  * Review individually: prompt once per group.\n"
            f"  * Dismiss: skip for now (will re-prompt next cycle).",
            buttons=['Dismiss', 'Review individually',
                     f'Auto-resolve all ({n})'])

        if choice == 0:
            return 'dismiss'
        if choice == 1:
            return 'review'
        return 'auto'

    def _autoResolveFirstAsMaster(self, path: str, ops: list) -> None:
        """Tag all but the first op in the group as clones.

        Applied when the user opts into batch resolution. Matches the
        common case where the first-listed operator is the desired
        master and the rest are copy-paste or drag-in duplicates.
        """
        if not ops:
            return
        master = ops[0]
        clones = ops[1:]
        for o in clones:
            self._handleDuplicateAsReference(o)
        plural = 's' if len(clones) != 1 else ''
        self.Log(
            f"Auto-resolved '{master.path}' as master for '{path}' "
            f"({len(clones)} clone{plural})", "SUCCESS")

    def _handleDuplicateAsReference(self, oper):
        """Mark duplicate as intentional clone reference."""
        oper.tags.add('clone')
        oper.color = (self.my.par.Clonetagcolorr,
                      self.my.par.Clonetagcolorg,
                      self.my.par.Clonetagcolorb)

        rel_file_path = self.getExternalPath(oper)

        # Add to table if not already present
        row_exists = any(
            self.Externalizations[row, 'path'] == oper.path
            for row in range(1, self.Externalizations.numRows)
        )

        if not row_exists:
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            if oper.family == 'COMP':
                strategy = 'tox'
                build_num = int(oper.par.Build.eval())
                touch_build = str(oper.par.Touchbuild.eval())
            else:
                strategy = oper.type
                build_num = ''
                touch_build = ''

            has_strategy_col = self.Externalizations[0, 'strategy'] is not None
            has_position_cols = self.Externalizations[0, 'node_x'] is not None

            node_x = str(int(oper.nodeX)) if has_position_cols else ''
            node_y = str(int(oper.nodeY)) if has_position_cols else ''
            node_color = ''
            if has_position_cols:
                c = oper.color
                node_color = f'{c[0]:.4f},{c[1]:.4f},{c[2]:.4f}'

            if has_strategy_col:
                row_data = [
                    oper.path, oper.type, strategy, rel_file_path,
                    timestamp, '', build_num, touch_build
                ]
                if has_position_cols:
                    row_data.extend([node_x, node_y, node_color])
                self.Externalizations.appendRow(row_data)
            else:
                self.Externalizations.appendRow([
                    oper.path, oper.type, rel_file_path, timestamp,
                    '', build_num, touch_build
                ])

        self.Log(f"Added 'clone' tag to {oper.path}", "SUCCESS")


    # ==========================================================================
    # TAGGING UI
    # ==========================================================================

    def TagGetter(self) -> None:
        """Open tagging menu for rollover operator."""
        if self._performMode:
            return
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

        # Untagged operator -- show tag selection
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

        # Show Embed Storage toggle (TDN COMPs only)
        btn_embed_storage = self.tagger.op('btn_embed_storage')
        if btn_embed_storage:
            btn_embed_storage.par.display = embed_visible
            if embed_visible:
                per_comp = oper.fetch('embed_storage_in_tdn', None, search=False)
                effective = per_comp if per_comp is not None else self.my.par.Embedstorageintdns.eval()
                btn_embed_storage.par.label = '\u229e  Embed storage in tdn  \u2713' if effective else '\u229e  Embed storage in tdn'
                btn_embed_storage.par.colorr = self.my.par.Taggingmenucolorr.eval()
                btn_embed_storage.par.colorg = self.my.par.Taggingmenucolorg.eval()
                btn_embed_storage.par.colorb = self.my.par.Taggingmenucolorb.eval()

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
        # (+ Embed DATs for TDN) (+ Embed Storage for TDN) (+ Open file if applicable)
        visible_count = 6 + (2 if embed_visible else 0) + (1 if rel_fp else 0)
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
        btn_embed_storage = self.tagger.op('btn_embed_storage')
        if btn_embed_storage:
            btn_embed_storage.par.display = False

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
        btn_embed_storage = self.tagger.op('btn_embed_storage')
        if btn_save:
            btn_save.par.display = False
        if btn_reload:
            btn_reload.par.display = False
        if btn_embed:
            btn_embed.par.display = False
        if btn_embed_storage:
            btn_embed_storage.par.display = False
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

            delete_file = self._shouldDeleteFile()
            if oper.family == 'COMP':
                if tag == self.my.par.Toxtag.val:
                    rel_file_path = self.getExternalPath(oper)
                    self.RemoveListerRow(oper.path, rel_file_path,
                                         delete_file=delete_file)
                    oper.par.externaltox = ''
                    oper.par.externaltox.readOnly = False
                elif tag == self.my.par.Tdntag.val:
                    self._removeTDNStrategy(oper.path,
                                            delete_file=delete_file)
            elif oper.family == 'DAT':
                rel_file_path = self.getExternalPath(oper)
                self.RemoveListerRow(oper.path, rel_file_path,
                                     delete_file=delete_file)
                oper.par.file = ''
                oper.par.file.readOnly = False

        return True

    def _shouldDeleteFile(self) -> bool:
        """Check the File Cleanup preference parameter.

        Returns True if external files should be deleted, False to keep them.
        When set to 'ask', shows a confirmation dialog.
        """
        filecleanup_par = getattr(self.my.par, 'Filecleanup', None)
        preference = filecleanup_par.eval() if filecleanup_par else 'ask'
        if preference == 'keep':
            return False
        elif preference == 'delete':
            return True
        else:  # 'ask'
            choice = ui.messageBox(
                'Delete External File?',
                'Also delete the external file from disk?',
                buttons=['Keep File', 'Delete File',
                         'Always Keep', 'Always Delete'])
            if choice == 0:
                return False
            elif choice == 1:
                return True
            elif choice == 2:
                if filecleanup_par:
                    self.my.par.Filecleanup = 'keep'
                    self.Log('File cleanup preference set to Always Keep', 'INFO')
                return False
            elif choice == 3:
                if filecleanup_par:
                    self.my.par.Filecleanup = 'delete'
                    self.Log('File cleanup preference set to Always Delete', 'INFO')
                return True
            else:
                return False  # Dialog closed

    def _removeCompStrategy(self, oper: OP, tag: str) -> None:
        """Remove a COMP strategy tag and clean up its externalization."""
        delete_file = self._shouldDeleteFile()
        oper.tags.discard(tag)
        if tag == self.my.par.Toxtag.val:
            rel_file_path = self.getExternalPath(oper)
            self.RemoveListerRow(oper.path, rel_file_path,
                                 delete_file=delete_file)
            oper.par.externaltox = ''
            oper.par.externaltox.readOnly = False
        elif tag == self.my.par.Tdntag.val:
            self._removeTDNStrategy(oper.path, delete_file=delete_file)

    def _removeTDNStrategy(self, op_path: str, delete_file: bool = True) -> None:
        """Remove TDN strategy entry from table and optionally delete .tdn file."""
        table = self.Externalizations
        if not table:
            self.Log(f"_removeTDNStrategy: no table!", "WARNING")
            return
        if table[0, 'strategy'] is None:
            self.Log(f"_removeTDNStrategy: no strategy column!", "WARNING")
            return  # Legacy table without strategy column -- no TDN entries
        self.Log(f"_removeTDNStrategy: searching for '{op_path}' delete_file={delete_file} rows={table.numRows}", "INFO")
        for i in range(1, table.numRows):
            if (self._cellVal(i, 'path') == op_path
                    and self._cellVal(i, 'strategy') == 'tdn'):
                rel_path = self._cellVal(i, 'rel_file_path')
                self.Log(f"_removeTDNStrategy: found row {i}, rel_path='{rel_path}' delete_file={delete_file}", "INFO")
                if delete_file and rel_path:
                    full_path = self.buildAbsolutePath(
                        self.normalizePath(rel_path)).resolve()
                    self.Debug(f"TDN delete: rel='{rel_path}' abs='{full_path}' exists={full_path.is_file()} suffix='{full_path.suffix}'")
                    def _delete(fp=full_path, rp=rel_path, opp=op_path):
                        try:
                            debug(f"_delete executing: {fp} exists={fp.is_file()}")
                            if fp.is_file() and fp.suffix.lower() == '.tdn':
                                fp.unlink()
                                self.Log(f'Removed TDN externalization for {opp} ({rp})', 'SUCCESS')
                            else:
                                debug(f"_delete skipped: is_file={fp.is_file()} suffix={fp.suffix}")
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
            child_path = self._cellVal(i, 'path')
            if child_path.startswith(prefix) and not op(child_path):
                rows_to_delete.append(i)

        # Delete in reverse order to preserve row indices
        for i in reversed(rows_to_delete):
            rel_file = self._cellVal(i, 'rel_file_path')
            self.Log(f"Removed orphaned child entry: {self._cellVal(i, 'path')}", "INFO")
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

    def HandleEmbedStorage(self, oper: OP) -> None:
        """Toggle per-COMP 'embed storage' setting and re-export the .tdn."""
        # Read current effective value
        per_comp = oper.fetch('embed_storage_in_tdn', None, search=False)
        if per_comp is not None:
            effective = per_comp
        else:
            effective = self.my.par.Embedstorageintdns.eval()

        # Toggle to explicit opposite
        new_val = not effective
        oper.store('embed_storage_in_tdn', new_val)

        # Re-export the .tdn with the new setting
        rel_tdn_path = self._getStrategyFilePath(oper.path, 'tdn')
        if rel_tdn_path:
            abs_path = str(self.buildAbsolutePath(rel_tdn_path))
            protected = self._getAllTrackedTDNFiles(exclude_path=oper.path)
            self.my.ext.TDN.ExportNetwork(
                root_path=oper.path, output_file=abs_path,
                cleanup_protected=protected)

        state = 'on' if new_val else 'off'
        self.Log(f"Embed storage set to {state} for {oper.path}", 'SUCCESS')
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
            self._removeExternalization(oper)

    def _removeExternalization(self, oper: OP) -> None:
        """Remove externalization from a COMP or DAT (no confirmation dialog).

        Deletes the external file, clears tags/parameters, removes the
        tracking entry, and resets operator color.
        """
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
            # Table-only TDN entry (e.g., Full Project export) -- no tag on operator
            self.RemoveTDNEntry(oper.path)

        self.resetOpColor(oper)
        self.Refresh()

    def _dispatchTaggerButton(self, oper: OP, tag: str,
                              label: str) -> None:
        """Route a tagger manage-mode button click to the correct handler.

        Determines the action from the button label text:
        - Labels containing 'Remove' -> remove externalization
        - Labels containing 'Convert to' -> convert DAT format
        - Otherwise -> switch COMP strategy (TOX<->TDN)

        Note: The caller (parexec1 in tagger buttons) is responsible for
        closing the tagger window and deferring if needed (e.g., to let
        the window close before showing a confirmation dialog).
        """
        if 'Remove' in label:
            self.HandleStrategyRemove(oper)
        elif 'Convert to' in label:
            self.HandleDATConvert(oper, tag)
        else:
            self.HandleStrategySwitch(oper)

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
            if (self._cellVal(i, 'path') == oper.path
                    and self._cellVal(i, 'strategy') == strategy):
                # Already tracked -- just re-save
                if is_tox:
                    self.Save(oper.path)
                elif is_tdn:
                    self.SaveTDN(oper.path)
                # DATs use syncfile -- no explicit save needed
                return

        # Not tracked -- full initialization (creates tracking entry + saves file)
        self.handleAddition(oper)

    # ==========================================================================
    # PROJECT-WIDE EXTERNALIZATION
    # ==========================================================================

    def ExternalizeProject(self) -> None:
        """Externalize all compatible COMPs and DATs in project."""
        if self._performMode:
            return
        choice = ui.messageBox('Embody -- Externalize Full Project',
            'Add all compatible COMPs and DATs to Embody?\n'
            '(Palette components, clones, and replicants will be ignored)\n\n'
            '  TOX: Externalize each COMP as a .tox file.\n'
            '  TDN: Externalize each COMP as a .tdn file.\n\n'
            'Optionally, also export a single project-wide .tdn\n'
            'snapshot of your entire network (Ctrl+Shift+E).',
            buttons=['Cancel', 'TOX', 'TDN', 'TOX + Project TDN',
                     'TDN + Project TDN'])

        if choice < 1:
            return

        use_tdn = choice in (2, 4)
        export_project_tdn = choice in (3, 4)

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

        # Export project-wide TDN snapshot if requested
        if export_project_tdn:
            self.my.ext.TDN.ExportNetworkAsync(
                output_file='auto', embed_all=True)

    def _shouldSkipOp(self, oper, paths_to_exclude):
        """Check if operator should be skipped in project externalization."""
        return (
            oper.path in paths_to_exclude or
            self.isReplicant(oper) or
            self.isInsideClone(oper) or
            self.my.ext.TDN._hasExcludeTag(oper) or
            oper.path.startswith('/local/') or
            oper.path == '/local'
        )

    # ==========================================================================
    # LISTER ROW REMOVAL
    # ==========================================================================

    def RemoveListerRow(self, op_path: str, rel_file_path: str, delete_file: bool = True) -> None:
        """
        Remove an operator from externalization tracking.
        SAFETY: Only deletes the file if it's tracked by Embody and not referenced elsewhere.
        When delete_file=False, the table row and tags are removed but the file is preserved on disk.
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
        # 1. delete_file is True (caller wants file removed)
        # 2. It's not a clone reference
        # 3. No other operators reference it
        # 4. It's a file we're tracking (implicit - we got rel_file_path from our table)
        if delete_file and normalized_path and not other_references and not is_clone:
            full_path = self.buildAbsolutePath(normalized_path).resolve()
            
            def _do_delete():
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

            run(_do_delete, delayFrames=5)
        elif is_clone or other_references:
            self.Log(f"Preserved file '{normalized_path}' (still in use)", "INFO")

        # Remove from table -- match on both path and rel_file_path to avoid
        # deleting sibling rows (e.g. a TDN row when removing the TOX row)
        removed = False
        for i in range(1, self.Externalizations.numRows):
            if (self._cellVal(i, 'path') == op_path
                    and self.normalizePath(self._cellVal(i, 'rel_file_path')) == normalized_path):
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
        mode = self._tdnMode()
        if mode == 'off':
            self.Log('TDN mode=off -- skipping reconstruction', 'INFO')
            return
        if mode == 'export':
            self.Log('TDN mode=export -- .toe is source of truth, skipping '
                     'reconstruction', 'INFO')
            return
        # mode == 'full'
        if not self.my.par.Tdncreateonstart.eval():
            return

        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return

        self.Log(f'Reconstructing {len(tdn_comps)} TDN COMP(s)...', 'INFO')
        errors_total = 0

        for comp_path, rel_tdn_path in tdn_comps:
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

            comp = op(comp_path)
            if comp is None:
                # COMP was tagged but .toe wasn't saved -- create the shell.
                # Prefer type from TDN file (v1.1+), then table, then 'base'.
                tdn_type = tdn_doc.get('type')
                comp = self._createMissingCompShell(
                    comp_path, 'tdn', comp_type_override=tdn_type)
                if comp is None:
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
                # Attempt rollback from backup .tdn
                try:
                    backup_path = self.my.ext.TDN._get_backup_path_instance(
                        str(abs_path))
                    if backup_path.is_file():
                        import json as _json
                        backup_tdn = _json.loads(
                            backup_path.read_text(encoding='utf-8'))
                        rb_result = self.my.ext.TDN.ImportNetwork(
                            target_path=comp_path, tdn=backup_tdn,
                            clear_first=True, restore_file_links=True)
                        if rb_result.get('success'):
                            self.Log(
                                f'Rolled back {comp_path} from backup',
                                'WARNING')
                            continue
                        else:
                            self.Log(
                                f'Rollback failed for {comp_path}: '
                                f'{rb_result.get("error")}', 'ERROR')
                except Exception as rb_e:
                    self.Log(
                        f'Rollback error for {comp_path}: {rb_e}', 'ERROR')
                errors_total += 1
                continue

            created = result.get('created_count', 0)
            restored = result.get('restored_file_links', 0)
            msg = f'Reconstructed {comp_path} ({created} ops'
            if restored:
                msg += f', {restored} file links'
            msg += ')'
            self.Log(msg, 'SUCCESS')

            # Reconstruct About page from TSV (no longer serialized in .tdn)
            self._reconstructAboutPage(comp, comp_path)

            # Prime dirty-detection baselines on the freshly reconstructed
            # (clean) network so the dirty indicator is accurate from project
            # open, rather than being set lazily by the first scan -- which
            # would absorb any edit made before it and wrongly read clean.
            # Mirrors _handleTDNAddition and SaveTDN (both snapshot here).
            self.param_tracker.updateParamStore(comp)
            self._storeTDNFingerprint(comp)

            # Phase E: Post-reconstruction error checking
            comp_errors = self._verifyReconstructedComp(comp)
            if comp_errors:
                errors_total += len(comp_errors)

        # Build report
        self._logReconstructionReport(tdn_comps, errors_total)

    # Params visible only in 'full' mode (strip/reconstruction concepts).
    _TDN_FULL_ONLY_PARAMS = {'Tdnstriponsave', 'Tdncreateonstart'}

    def _tdnMode(self) -> str:
        """Return 'off' | 'export' | 'full' from Tdnmode menu.

        Defaults to 'export' if the parameter is missing (legacy .tox).
        """
        par = getattr(self.my.par, 'Tdnmode', None)
        if par is None:
            return 'export'
        try:
            val = par.eval()
            return val if val in ('off', 'export', 'full') else 'export'
        except Exception:
            return 'export'

    def _tdnEnabled(self) -> bool:
        """Return True if the TDN subsystem is NOT in Off mode.

        Thin wrapper for call sites that only need to know whether any
        TDN runtime behavior should fire (export OR strip). Callers that
        need to distinguish export vs full should use _tdnMode().
        """
        return self._tdnMode() != 'off'

    # ==========================================================================
    # PERFORM MODE
    # ==========================================================================

    @property
    def _performMode(self) -> bool:
        """True when Perform Mode is active -- all compute suppressed."""
        par = getattr(self.my.par, 'Performmode', None)
        return bool(par.eval()) if par is not None else False

    def _enterPerformMode(self) -> None:
        """Suspend all Embody features for live performance."""
        # Snapshot state so we can restore on exit
        state = {
            'envoy_was_running': bool(self.my.fetch('envoy_running', False, search=False)),
            'kb_active': self.my.op('keyboardin1').par.active.eval(),
            'exit_tagger_active': self.my.op('chopexec_exit_tagger').par.active.eval(),
        }
        self.my.store('_perform_state', state)

        # Stop Envoy directly (do NOT touch Envoyenable -- that would corrupt config.json)
        self.my.ext.Envoy.Stop()

        # Disable keyboard shortcuts and exit tagger
        self.my.op('keyboardin1').par.active = False
        self.my.op('chopexec_exit_tagger').par.active = False

        # Close manager window if open
        self.my.op('window_manager').par.winclose.pulse()

        # Update status display
        self.my.par.Envoystatus = 'Perform Mode'

        # Grey out Envoy parameters so user sees they're frozen
        for p in ('Envoyenable', 'Envoyport', 'Aiclient', 'Aiprojectroot', 'Aiprojectrootcustom'):
            par = getattr(self.my.par, p, None)
            if par is not None:
                par.enable = False

        self.Log('Perform Mode ON -- features suspended', 'INFO')

    def _exitPerformMode(self) -> None:
        """Restore all Embody features after live performance."""
        state = self.my.fetch('_perform_state', {}, search=False)

        # Re-enable keyboard shortcuts and exit tagger
        self.my.op('keyboardin1').par.active = state.get('kb_active', True)
        self.my.op('chopexec_exit_tagger').par.active = state.get('exit_tagger_active', True)

        # Restore Envoy parameter enable state
        for p in ('Envoyenable', 'Envoyport', 'Aiclient', 'Aiprojectroot', 'Aiprojectrootcustom'):
            par = getattr(self.my.par, p, None)
            if par is not None:
                par.enable = True

        # Restart Envoy if it was running before
        if state.get('envoy_was_running'):
            run("parent.Embody.ext.Envoy.Start()", delayFrames=5)

        # Clean up snapshot
        self.my.unstore('_perform_state')

        # Trigger Refresh to restore UI state
        run("parent.Embody.par.Refresh.pulse()", delayFrames=10)

        self.Log('Perform Mode OFF -- features restored', 'INFO')

    def _applyTdnModeGating(self) -> None:
        """Three-way UI gating for TDN-page parameters based on Tdnmode.

        - Off: all params greyed except Tdnmode itself.
        - Export: strip/reconstruction params (Tdnstriponsave, Tdncreateonstart)
          greyed; remaining Embed/cascade/picker params stay live.
        - Full: all params live.
        """
        master = getattr(self.my.par, 'Tdnmode', None)
        if master is None:
            return
        mode = self._tdnMode()
        try:
            for page in self.my.customPages:
                if page.name != 'TDN':
                    continue
                for p in page.pars:
                    if p.name == 'Tdnmode':
                        continue
                    try:
                        if mode == 'off':
                            p.enable = False
                        elif mode == 'export':
                            p.enable = p.name not in self._TDN_FULL_ONLY_PARAMS
                        else:  # full
                            p.enable = True
                    except Exception:
                        pass
        except Exception as e:
            self.Log(f'Could not apply Tdnmode gating: {e}', 'DEBUG')

    # Backward-compat alias (old name used inside Update / parexec history).
    _applyTdnEnableGating = _applyTdnModeGating

    def _onTdnModeChanged(self, mode: str) -> None:
        """Handle a Tdnmode change from parexec.

        Transitions surface the impact so the user isn't surprised:
        - TO off with tracked TDN COMPs: confirmation dialog (preserve files).
        - export -> full: INFO log that Full is experimental.
        - full -> export: INFO log that reconstruction will be skipped.
        - off -> full: no dialog here (cold flip).

        Always refreshes gating last.
        """
        if mode == 'off':
            existing = []
            try:
                existing = self._getTDNStrategyComps()
            except Exception as e:
                self.Log(f'Could not enumerate TDN COMPs: {e}', 'DEBUG')
            if existing:
                count = len(existing)
                choice = self._messageBox(
                    'Embody - Disable TDN',
                    f'Switching TDN to Off with {count} tracked TDN COMP(s).\n\n'
                    f'Their .tdn files on disk will be preserved. Embody will\n'
                    f'simply stop reconstructing, stripping, or re-exporting\n'
                    f'them until you switch back.\n\n'
                    f'Continue?',
                    buttons=['Cancel', 'Keep .tdn files (disable only)'])
                if choice != 1:
                    # User cancelled -- restore to Export (the safe default)
                    # with parexec suppressed so _onTdnModeChanged doesn't
                    # re-fire and log a misleading "mode: Export-on-Save".
                    parexec = self.my.op('parexec')
                    was_active = (parexec.par.active.eval()
                                  if parexec else None)
                    if parexec:
                        parexec.par.active = False
                    try:
                        self.my.par.Tdnmode = 'export'
                    finally:
                        if parexec:
                            parexec.par.active = was_active
                    self._applyTdnModeGating()
                    self.Log('TDN mode change cancelled by user', 'INFO')
                    return
                self.Log('TDN disabled (.tdn files preserved on disk)',
                         'INFO')
            # else: no tracked COMPs -- flip is silent, nothing to preserve
        elif mode == 'full':
            self.Log(
                'TDN mode: Roundtrip (Experimental). Strip/restore '
                'runs on save; children are reconstructed from .tdn on open. '
                'Watch for edge cases with extension reload timing on '
                'deeply-nested TDN COMPs.', 'INFO')
        elif mode == 'export':
            self.Log(
                'TDN mode: Export-on-Save. .toe is the source of truth; '
                '.tdn files are rewritten on save. Reconstruction on open '
                'is skipped.', 'INFO')
        self._applyTdnModeGating()

    # Backward-compat alias (old name referenced by parexec pre-rename).
    _onTdnEnableChanged = _onTdnModeChanged

    def _getTDNStrategyComps(self) -> list[tuple[str, str]]:
        """Get all TDN-strategy COMPs from the externalizations table.

        Returns list of (comp_path, rel_tdn_path) tuples.
        Never includes Embody itself, its ancestors, or its descendants --
        reconstructing or stripping anything inside Embody would be
        self-destruction.
        """
        table = self.Externalizations
        if not table:
            return []
        if table[0, 'strategy'] is None:
            return []  # Legacy table without strategy column -- no TDN entries
        embody_path = self.my.path  # e.g. /embody/Embody -- skip regardless of location
        result = []
        for i in range(1, table.numRows):
            if self._cellVal(i, 'strategy') == 'tdn':
                comp_path = self._cellVal(i, 'path')
                # Never include root "/" -- stripping it destroys the entire project.
                # Never include Embody, its ancestors, or its descendants.
                if (comp_path == '/'
                        or comp_path == embody_path
                        or embody_path.startswith(comp_path + '/')
                        or comp_path.startswith(embody_path + '/')):
                    continue
                # Never reconstruct/strip a COMP tagged for exclusion -- the
                # owning app owns its lifecycle. Defends against a stale row
                # left from before the exclude tag was applied.
                comp = op(comp_path)
                if comp is not None and self.my.ext.TDN._hasExcludeTag(comp):
                    continue
                result.append((
                    comp_path,
                    self._cellVal(i, 'rel_file_path'),
                ))
        # Sort by path depth (fewest segments first) so parents are
        # imported before their children during reconstruction. Each
        # child's own .tdn file then overwrites the parent's snapshot.
        result.sort(key=lambda x: x[0].count('/'))
        return result

    # ------------------------------------------------------------------
    # DAT Content Safety
    # ------------------------------------------------------------------

    # DAT operator types whose `text`/table content is fully derived by
    # TouchDesigner from inputs, parameters, or runtime state. The user
    # cannot author this content -- TD regenerates it on cook -- so
    # warning that it "will be lost on save" is noise. Compared against
    # `dat.type` (short form, e.g. 'info' not 'infoDAT'), matching the
    # convention used by self.supported_dat_types.
    #
    # Callback DATs (execute, parexec, chopexec, datexec, opexec,
    # panelexec, pargroupexec, keyboardin, mousein, oscin, etc.) are
    # NOT in this set -- their content IS user-authored Python and must
    # continue to surface in the at-risk warning.
    _TD_MANAGED_DAT_TYPES = {
        'info',           # Info DAT -- introspection of another op
        'webrtc',         # Per-connection signaling state
        'folder',         # Filesystem listing
        'opfind',         # Network search results
        'monitors',       # Monitor hardware state
        'audiodevices',   # Audio device enumeration
        'videodevices',   # Video device enumeration
        'serialdevices',  # Serial device enumeration
        'mididevices',    # MIDI device enumeration
        'midievent',      # Project-wide MIDI event log
        'error',          # FIFO of recent TD errors
        'perform',        # Cook/draw timing log
        'examine',        # Inspector view of another op
        'mediafileinfo',  # Metadata extracted from a media file
        'tuioin',         # Inbound TUIO event table
        'multitouchin',   # Inbound Windows multi-touch events
        'ndi',            # Discovered NDI sources
        'mpcdi',          # Calibration data parsed from .mpcdi
        'indices',        # Generated number series
    }

    def _findAtRiskDATs(self) -> list:
        """Find DATs inside TDN COMPs that will lose content during save.

        Returns list of (comp_path, [dat_ops]) tuples for TDN COMPs where
        Embed DATs is OFF and unexternalized DATs have non-empty content.
        """
        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return []

        tdn_paths = {path for path, _ in tdn_comps}
        dat_tags = set(self.getTags('DAT'))
        result = []

        for comp_path, _ in tdn_comps:
            comp = op(comp_path)
            if not comp:
                continue

            # Resolve embed_dats: per-COMP override -> global parameter
            per_comp = comp.fetch('embed_dats_in_tdn', None, search=False)
            embed_on = (per_comp if per_comp is not None
                        else self.my.par.Embeddatsintdns.eval())
            if embed_on:
                continue  # Content will be preserved in TDN

            at_risk = []
            for dat in comp.findChildren(type=DAT):
                # Skip DATs inside a deeper TDN COMP -- covered by that
                # COMP's own settings
                inside_nested = False
                parent_op = dat.parent()
                while parent_op and parent_op.path != comp_path:
                    # Skip DATs inside a deeper TDN COMP (its own settings
                    # cover them) or inside an excluded COMP (app-managed,
                    # invisible to TDN -- never at risk).
                    if (parent_op.path in tdn_paths
                            or self.my.ext.TDN._hasExcludeTag(parent_op)):
                        inside_nested = True
                        break
                    parent_op = parent_op.parent()
                if inside_nested:
                    continue

                # Skip DATs that already have an Embody tag
                if dat.tags & dat_tags:
                    continue

                # Skip DATs with a file parameter already set
                if hasattr(dat.par, 'file') and dat.par.file.eval():
                    continue

                # Skip DATs whose content TD generates and regenerates
                # on cook (info, webrtc, folder, monitors, devices, etc.)
                # The user did not author this content and cannot preserve
                # it -- warning would be noise. Callback DATs (execute,
                # parexec, etc.) are intentionally absent from this set.
                if dat.type in self._TD_MANAGED_DAT_TYPES:
                    continue

                # Check for non-empty content
                try:
                    if dat.isTable:
                        if dat.numRows > 0:
                            at_risk.append(dat)
                    else:
                        if dat.text and dat.text.strip():
                            at_risk.append(dat)
                except Exception:
                    pass  # Unreadable DAT -- skip

            if at_risk:
                result.append((comp_path, at_risk))

        return result

    # Storage keys preserved even when Embedstorageintdns is off
    # (mirrors TDNExt logic that exports these as control metadata).
    _STORAGE_CONTROL_KEYS = {'embed_dats_in_tdn', 'embed_storage_in_tdn'}
    # Storage keys never surfaced as at-risk -- superset of
    # TDNExt.SKIP_STORAGE_KEYS covering additional Embody runtime state
    # (mode migration flags, pane restore, init completion, etc.) that
    # TDNExt also does not serialize meaningfully. Only user-owned keys
    # should reach _findAtRiskStorage.
    _STORAGE_SKIP_KEYS = {
        '_tdn_stripped_paths', '_git_root',
        'envoy_running', 'envoy_shutdown_event',
        'expanded_paths', 'expand_order',
        'manage_file_path', 'visible_count', 'hover',
        '_tdn_external_wires', '_tdn_pane_restore',
        '_tdn_palette_handling',
        '_init_complete', '_smoke_test_responses',
        '_tdn_restore_failures',
        '_tdn_mode_migration_shown', '_tdn_migration_scheduled',
        '_tdn_migration_prev_enable',
        'pressed',
    }

    def _findAtRiskStorage(self) -> list:
        """Find operators inside TDN COMPs whose comp.storage entries will
        be lost on save. Mirrors _findAtRiskDATs.

        Returns list of (comp_path, [(op_path, [keys])]) tuples for TDN
        COMPs where Embed Storage is OFF and any op inside has non-control,
        non-runtime storage keys.
        """
        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return []

        tdn_paths = {path for path, _ in tdn_comps}
        result = []

        for comp_path, _ in tdn_comps:
            comp = op(comp_path)
            if not comp:
                continue

            # Resolve embed_storage: per-COMP override -> global parameter
            per_comp = comp.fetch('embed_storage_in_tdn', None, search=False)
            embed_on = (per_comp if per_comp is not None
                        else self.my.par.Embedstorageintdns.eval())
            if embed_on:
                continue  # Storage preserved in TDN

            at_risk = []
            # Check comp itself and all descendants (depth is unbounded;
            # excluded descendants are only those inside a nested TDN COMP,
            # which that COMP's own settings handle).
            candidates = [comp] + list(comp.findChildren())
            for target in candidates:
                # Skip excluded COMPs themselves -- app-managed, invisible
                # to TDN, never at risk.
                if self.my.ext.TDN._hasExcludeTag(target):
                    continue
                # Skip ops inside a nested TDN COMP or inside an excluded COMP
                if target is not comp:
                    inside_nested = False
                    parent_op = target.parent()
                    while parent_op and parent_op.path != comp_path:
                        if (parent_op.path in tdn_paths
                                or self.my.ext.TDN._hasExcludeTag(parent_op)):
                            inside_nested = True
                            break
                        parent_op = parent_op.parent()
                    if inside_nested:
                        continue

                try:
                    storage = target.storage
                except Exception:
                    continue
                if not storage:
                    continue

                risky_keys = [
                    k for k in storage.keys()
                    if k not in self._STORAGE_CONTROL_KEYS
                    and k not in self._STORAGE_SKIP_KEYS
                ]
                if risky_keys:
                    at_risk.append((target.path, sorted(risky_keys)))

            if at_risk:
                result.append((comp_path, at_risk))

        return result

    def _promptTDNContentSafety(
            self, at_risk_dats: list, at_risk_storage: list) -> str:
        """Show combined dialog for at-risk DATs + storage.

        Returns 'externalize' or 'skip'. Note: 'externalize' applies only
        to DATs; storage has no externalization path, skip logs a summary.
        """
        all_dats = [d for _, dats in at_risk_dats for d in dats]
        dat_count = len(all_dats)
        storage_entries = [
            (op_path, keys)
            for _, entries in at_risk_storage
            for op_path, keys in entries
        ]
        storage_count = sum(len(keys) for _, keys in storage_entries)

        sections = []

        if dat_count:
            noun = 'DAT' if dat_count == 1 else 'DATs'
            lines = []
            for dat in all_dats[:10]:
                fmt = 'table' if dat.isTable else 'text'
                lines.append(f'  \u2022 {dat.path} ({fmt})')
            if dat_count > 10:
                lines.append(f'  \u2026 and {dat_count - 10} more')
            sections.append(
                f'{dat_count} {noun} will lose content (Embed DATs OFF):\n'
                + '\n'.join(lines))

        if storage_count:
            key_noun = 'key' if storage_count == 1 else 'keys'
            lines = []
            shown = 0
            for op_path, keys in storage_entries:
                for k in keys:
                    if shown >= 10:
                        break
                    lines.append(f'  \u2022 {op_path} \u2192 "{k}"')
                    shown += 1
                if shown >= 10:
                    break
            if storage_count > 10:
                lines.append(f'  \u2026 and {storage_count - 10} more')
            sections.append(
                f'{storage_count} storage {key_noun} will be lost '
                f'(Embed Storage OFF):\n' + '\n'.join(lines))

        body = '\n\n'.join(sections)
        externalize_verb = 'Externalize DATs' if dat_count else 'Continue'
        msg = (f'TDN content will be dropped on next save.\n\n'
               f'{body}\n\n'
               f'Note: storage has no externalization path -- enable Embed '
               f'Storage in TDNs to preserve it, or dismiss to proceed.\n\n'
               f'"Always" choices are remembered (revert anytime via the '
               f'TDN content-safety parameter on Embody).')

        buttons = [externalize_verb, 'Always Externalize',
                   'Skip Once', 'Always Skip']
        choice = self._messageBox(
            'TDN Content at Risk', msg, buttons=buttons)

        if choice == 0:
            return 'externalize'
        elif choice == 1:
            self.my.par.Tdndatsafety = 'externalize'
            self.Log('TDN content safety preference set to Always '
                     'Externalize', 'INFO')
            return 'externalize'
        elif choice == 3:
            self.my.par.Tdndatsafety = 'ignore'
            self.Log('TDN content safety preference set to Always Skip '
                     '-- save-time warnings disabled (re-enable via the '
                     'TDN content-safety parameter on Embody)', 'INFO')
            return 'skip'
        return 'skip'

    def _externalizeDATs(self, dats: list) -> int:
        """Bulk-externalize a list of DAT operators. Returns success count."""
        count = 0
        for dat in dats:
            try:
                # Infer tag from DAT type
                tag_par_name = self.dat_type_to_tag.get(dat.type)
                if not tag_par_name:
                    continue
                tag_value = getattr(self.my.par, tag_par_name).val
                if not tag_value:
                    continue

                self.applyTagToOperator(dat, tag_value)
                self.ExternalizeImmediate(dat)
                count += 1
            except Exception as e:
                self.Log(f'Failed to externalize {dat.path}: {e}', 'WARNING')
        return count

    def _checkTDNContentSafety(self) -> None:
        """Check for at-risk DATs AND storage in TDN COMPs.

        Called from onProjectPreSave() before the TDN export/strip cycle.
        Prompts user or auto-externalizes per Tdndatsafety preference.
        On skip, logs a SUCCESS summary naming what was dropped.
        """
        safety_par = getattr(self.my.par, 'Tdndatsafety', None)
        preference = safety_par.eval() if safety_par else 'ask'

        if preference == 'ignore':
            return

        at_risk_dats = self._findAtRiskDATs()
        at_risk_storage = self._findAtRiskStorage()
        if not at_risk_dats and not at_risk_storage:
            return

        all_dats = [d for _, dats in at_risk_dats for d in dats]

        if preference == 'externalize':
            count = self._externalizeDATs(all_dats)
            if count:
                self.Log(f'Auto-externalized {count} at-risk DAT(s)',
                         'SUCCESS')
            if at_risk_storage:
                self._logSkippedStorage(at_risk_storage)
            return

        # preference == 'ask'
        choice = self._promptTDNContentSafety(at_risk_dats, at_risk_storage)
        if choice == 'externalize':
            count = self._externalizeDATs(all_dats)
            self.Log(f'Externalized {count} at-risk DAT(s)', 'SUCCESS')
            if at_risk_storage:
                self._logSkippedStorage(at_risk_storage)
        else:
            if all_dats:
                self._logSkippedDATs(all_dats)
            if at_risk_storage:
                self._logSkippedStorage(at_risk_storage)

    # Backwards-compatible alias (execute.py may still call the old name).
    _checkDATContentSafety = _checkTDNContentSafety

    def _logSkippedDATs(self, dats: list) -> None:
        """Log a SUCCESS-level summary of DATs whose content was dropped."""
        names = ', '.join(d.path for d in dats[:5])
        if len(dats) > 5:
            names += f', \u2026 (+{len(dats) - 5} more)'
        self.Log(
            f'Skipped externalization of {len(dats)} at-risk DAT(s): '
            f'{names}', 'SUCCESS')

    def _logSkippedStorage(self, at_risk_storage: list) -> None:
        """Log a SUCCESS-level summary of storage keys that will be dropped."""
        entries = []
        total = 0
        for _, op_entries in at_risk_storage:
            for op_path, keys in op_entries:
                total += len(keys)
                entries.append(f'{op_path}[{",".join(keys)}]')
        shown = ', '.join(entries[:5])
        if len(entries) > 5:
            shown += f', \u2026 (+{len(entries) - 5} more)'
        self.Log(
            f'Dropping {total} TDN storage entr{"y" if total == 1 else "ies"} '
            f'on save (Embed Storage OFF): {shown}', 'SUCCESS')

    def StripCompChildren(self, comp: OP) -> int:
        """Remove children from a TDN-strategy COMP (for smaller .toe).

        Destroys both regular children and utility operators (annotations).
        Before destruction, captures external sibling wires on comp's own
        connectors and stores them on comp via comp.store() so they can
        be restored after the COMP is rebuilt (on post-save, cold open,
        or user reload). Storage survives .toe save since the COMP shell
        itself is not stripped.

        Returns the number of operators destroyed.
        """
        # Capture external connections before destroying children.
        # The in*/out* ops inside comp define its own connectors --
        # destroying them severs any external wires attached to them.
        try:
            externals = self.my.ext.TDN._captureExternalConnections(comp)
            if externals:
                comp.store('_tdn_external_wires', externals)
                self.Log(
                    f'Captured {len(externals)} external connection(s) on '
                    f'{comp.path} before strip', 'DEBUG')
        except Exception as e:
            self.Log(
                f'External capture failed on {comp.path}: {e}', 'WARNING')

        # findChildren with includeUtility=True gets everything:
        # regular children + hidden utility ops (annotations with utility=True)
        all_ops = list(comp.findChildren(depth=1, includeUtility=True))
        # Preserve excluded COMPs -- they are invisible to TDN and absent
        # from the .tdn, so stripping them would lose them permanently (the
        # post-save restore rebuilds from the .tdn, which omits them). The
        # owning application owns their lifecycle.
        excluded_paths = {c.path for c in all_ops
                          if self.my.ext.TDN._hasExcludeTag(c)}
        destroy_ops = [c for c in all_ops if c.path not in excluded_paths]
        if excluded_paths:
            self.Log(
                f'Preserving {len(excluded_paths)} excluded COMP(s) during '
                f'strip of {comp.path}', 'DEBUG')
        count = len(destroy_ops)
        n_utility = sum(1 for c in destroy_ops if getattr(c, 'utility', False))
        # Clear dock relationships pointing INTO the destroy set before
        # destroying -- TD's engine raises an uncatchable tdError if a dock
        # target is destroyed before its docked operator. This MUST include
        # a preserved excluded child docked to a soon-destroyed sibling.
        for child in all_ops:
            try:
                if (child.dock is not None
                        and child.dock.path not in excluded_paths):
                    child.dock = None
            except Exception:
                pass
        for child in destroy_ops:
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
            for child in comp.findChildren():
                err_str = child.errors()
                if err_str:
                    for err in err_str.split('\n'):
                        err = err.strip()
                        if err:
                            errors.append(f'{child.path}: {err}')
                warn_str = child.warnings()
                if warn_str:
                    for warn in warn_str.split('\n'):
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

    def _createMissingCompShell(self, comp_path: str, strategy: str,
                               comp_type_override: str = None) -> 'OP | None':
        """Create a missing COMP that was tagged but not saved in the .toe.

        Used by both ReconstructTDNComps and RestoreTOXComps when a tracked
        COMP doesn't exist on project open.

        Args:
            comp_path: Full TD path (e.g., '/embody/base_tdn')
            strategy: 'tdn' or 'tox' -- determines which tag/color to apply
            comp_type_override: Full TD type string (e.g. 'containerCOMP')
                from TDN file. Takes priority over externalizations table.

        Returns:
            The created COMP, or None on failure.
        """
        parent_path = comp_path.rsplit('/', 1)[0] or '/'
        parent_op = op(parent_path)
        if not parent_op or not hasattr(parent_op, 'create'):
            self.Log(f'Cannot create {comp_path}: parent {parent_path} '
                     f'not found or not a COMP', 'WARNING')
            return None

        # Priority: TDN type override > externalizations table > 'baseCOMP'
        if comp_type_override:
            td_type = comp_type_override
        else:
            comp_type = self._getCompTypeFromTable(comp_path) or 'base'
            td_type = f'{comp_type}COMP'
        comp_name = comp_path.rsplit('/', 1)[-1]

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
            if self._cellVal(i, 'path') == comp_path:
                return self._cellVal(i, 'type')
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
            if self._cellVal(i, 'path') == comp_path:
                x_val = self._cellVal(i, 'node_x')
                y_val = self._cellVal(i, 'node_y')
                if x_val and y_val:
                    try:
                        comp.nodeX = int(float(x_val))
                        comp.nodeY = int(float(y_val))
                    except (ValueError, TypeError):
                        pass
                color_val = self._cellVal(i, 'node_color')
                if color_val:
                    try:
                        r, g, b = [float(c) for c in color_val.split(',')]
                        comp.color = (r, g, b)
                    except (ValueError, TypeError):
                        pass
                return

    # ==========================================================================
    # METADATA RECONCILIATION ON START
    # ==========================================================================

    def ReconcileMetadata(self) -> None:
        """Re-apply tags, colors, and file parameters from the externalizations table.

        Handles the case where the user tagged operators (writing to the table
        on disk) but closed TD without saving (Ctrl+S).  The .toe retains the
        operators but loses their in-memory Embody metadata.  This method reads
        the table and re-applies any missing metadata so the session stays in
        sync with the on-disk source of truth.
        """
        # Skip ONLY when Embody is explicitly Disabled. Same race fix as
        # Update() -- transient 'Scanning defaults', 'Scanning palette',
        # and 'Testing' values must NOT block normal operation.
        if self.my.par.Status == 'Disabled':
            return

        table = self.Externalizations
        if not table or table.numRows < 2:
            return

        tox_tag = self.my.par.Toxtag.val
        tdn_tag = self.my.par.Tdntag.val
        embody_path = self.my.path
        reconciled = 0

        for i in range(1, table.numRows):
            path = self._cellVal(i, 'path')
            strategy = self._cellVal(i, 'strategy') if table[0, 'strategy'] is not None else ''
            rel_file_path = self._cellVal(i, 'rel_file_path')
            node_color = self._cellVal(i, 'node_color') if table[0, 'node_color'] is not None else ''

            # Skip Embody itself and its descendants
            if path == embody_path or path.startswith(embody_path + '/'):
                continue

            oper = op(path)
            if oper is None:
                continue  # Missing ops handled by RestoreTOXComps / ReconstructTDNComps

            # Determine expected tag from strategy
            if strategy == 'tox':
                tag = tox_tag
            elif strategy == 'tdn':
                tag = tdn_tag
            else:
                tag = strategy  # DAT strategies are the tag value (py, md, tsv, etc.)

            if not tag:
                continue

            # Check if already reconciled (idempotency)
            tag_present = tag in oper.tags
            if strategy == 'tox':
                if tag_present and oper.par.externaltox.eval():
                    continue
            elif strategy == 'tdn':
                if tag_present:
                    continue
            else:  # DAT
                if tag_present and oper.par.file.eval():
                    continue

            # --- Apply metadata ---
            if strategy not in ('tox', 'tdn'):
                # DAT reconciliation
                oper.tags.add(tag)
                self._setDATLanguageForTag(oper, tag)
                oper.par.file.readOnly = False
                oper.par.file = rel_file_path
                oper.par.syncfile = True
                oper.par.file.readOnly = True

            elif strategy == 'tox':
                # TOX COMP reconciliation
                oper.tags.add(tag)
                oper.par.externaltox.readOnly = False
                oper.par.externaltox = rel_file_path
                oper.par.externaltox.readOnly = True
                oper.par.enableexternaltox = True
                oper.par.reloadtoxpulse.pulse()
                self._restorePositionFromTable(oper, path)

            elif strategy == 'tdn':
                # TDN COMP reconciliation
                oper.tags.add(tag)
                self._restorePositionFromTable(oper, path)

            # Apply color: prefer table value, fall back to tag color
            color_applied = False
            if node_color:
                try:
                    r, g, b = [float(c) for c in node_color.split(',')]
                    oper.color = (r, g, b)
                    color_applied = True
                except (ValueError, TypeError):
                    pass
            if not color_applied:
                color = self._getTagColor(oper, tag)
                if color:
                    oper.color = color

            reconciled += 1
            self.Log(f"Reconciled '{path}' ({strategy})", "INFO")

        if reconciled:
            self.Log(f"Reconciled metadata on {reconciled} operator(s)", "SUCCESS")
        else:
            self.Log("All operator metadata consistent", "DEBUG")

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
                continue  # Already exists in .toe -- nothing to do
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
            if self._cellVal(i, 'strategy') == 'tox':
                comp_path = self._cellVal(i, 'path')
                # Never include Embody, its ancestors, or its descendants
                if (comp_path == '/'
                        or comp_path == embody_path
                        or embody_path.startswith(comp_path + '/')
                        or comp_path.startswith(embody_path + '/')):
                    continue
                result.append((
                    comp_path,
                    self._cellVal(i, 'rel_file_path'),
                    self._cellVal(i, 'type'),
                ))
        # Sort by path depth -- parents first
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
    # DAT RESTORATION ON START
    # ==========================================================================

    def RestoreDATs(self) -> None:
        """Restore missing DATs from externalized files on project open.

        For each DAT-strategy entry in the externalizations table where the
        operator is missing but the source file exists on disk, creates the
        correct DAT type and configures file/syncfile for auto-sync.
        """
        if not self.my.par.Datrestoreonstart.eval():
            return

        dat_entries = self._getDATEntries()
        if not dat_entries:
            return

        # Supported DAT types (matches self.supported_dat_types)
        valid_dat_types = set(self.supported_dat_types)

        # Filter to only missing DATs with existing files on disk
        to_restore = []
        for dat_path, rel_file_path, dat_type, strategy in dat_entries:
            if op(dat_path):
                continue  # Already exists in network
            abs_path = self.buildAbsolutePath(rel_file_path)
            if not abs_path.is_file():
                self.Log(f'File not found for missing DAT '
                         f'{dat_path}: {rel_file_path}', 'WARNING')
                continue
            to_restore.append((dat_path, rel_file_path, dat_type, strategy))

        if not to_restore:
            return

        self.Log(f'Restoring {len(to_restore)} DAT(s) from disk...', 'INFO')
        restored = 0
        errors = 0

        for dat_path, rel_file_path, dat_type, strategy in to_restore:
            # Check if it appeared (e.g. loaded as child of a parent .tox)
            if op(dat_path):
                restored += 1
                self.Log(f'DAT {dat_path} already present '
                         f'(loaded from parent)', 'INFO')
                continue

            # Verify parent exists and is a COMP
            parent_path = dat_path.rsplit('/', 1)[0] or '/'
            parent_op = op(parent_path)
            if not parent_op:
                self.Log(f'Parent {parent_path} not found, cannot restore '
                         f'{dat_path}', 'WARNING')
                errors += 1
                continue

            if not hasattr(parent_op, 'create'):
                self.Log(f'Parent {parent_path} is not a COMP, cannot restore '
                         f'{dat_path}', 'WARNING')
                errors += 1
                continue

            if dat_type not in valid_dat_types:
                self.Log(f'Unknown DAT type "{dat_type}" for '
                         f'{dat_path}', 'WARNING')
                errors += 1
                continue

            dat_name = dat_path.rsplit('/', 1)[-1]
            td_type = f'{dat_type}DAT'
            try:
                new_dat = parent_op.create(td_type, dat_name)
            except Exception as e:
                self.Log(f'Failed to create {dat_path} '
                         f'(type {td_type}): {e}', 'ERROR')
                errors += 1
                continue

            try:
                # Configure file sync
                normalized = self.normalizePath(rel_file_path)
                new_dat.par.file = normalized
                new_dat.par.syncfile = True
                new_dat.par.file.readOnly = True

                # Kick syncfile to force TD to read from disk
                op_path = str(new_dat)
                run(lambda p=op_path: self._safeSyncFile(p, False),
                    delayFrames=1)
                run(lambda p=op_path: self._safeSyncFile(p, True),
                    delayFrames=2)

                # Set language/extension for text DATs
                self._setDATLanguageForTag(new_dat, strategy)

                # Apply tag and color
                if strategy:
                    new_dat.tags.add(strategy)
                new_dat.color = (self.my.par.Dattagcolorr.eval(),
                                 self.my.par.Dattagcolorg.eval(),
                                 self.my.par.Dattagcolorb.eval())

                # Restore position from table metadata
                self._restorePositionFromTable(new_dat, dat_path)

                restored += 1
                self.Log(f'Restored {dat_path} from {rel_file_path}',
                         'SUCCESS')

            except Exception as e:
                self.Log(f'Failed to configure DAT {dat_path}: {e}', 'ERROR')
                errors += 1

        self._logDATRestorationReport(len(to_restore), restored, errors)

    def _getDATEntries(self) -> list[tuple[str, str, str, str]]:
        """Get all DAT-strategy entries from the externalizations table.

        Returns list of (dat_path, rel_file_path, dat_type, strategy) tuples,
        sorted by path depth (shallowest first).

        Never includes Embody itself or its descendants.
        Excludes DATs inside TOX-strategy or TDN-strategy COMPs
        (those are handled by RestoreTOXComps / ReconstructTDNComps).
        """
        table = self.Externalizations
        if not table:
            return []
        if table[0, 'strategy'] is None:
            return []  # Legacy table without strategy column

        embody_path = self.my.path

        # Collect TOX/TDN COMP paths so we can skip DATs inside them
        comp_paths = set()
        for i in range(1, table.numRows):
            strategy = self._cellVal(i, 'strategy')
            if strategy in ('tox', 'tdn'):
                comp_paths.add(self._cellVal(i, 'path'))

        result = []
        for i in range(1, table.numRows):
            strategy = self._cellVal(i, 'strategy')
            if strategy in ('tox', 'tdn', ''):
                continue  # COMP strategies or empty

            dat_path = self._cellVal(i, 'path')
            if not dat_path:
                continue

            # Never include Embody or its descendants
            if (dat_path == embody_path
                    or dat_path.startswith(embody_path + '/')):
                continue

            # Skip DATs inside TOX/TDN COMPs
            inside_comp = any(
                dat_path.startswith(cp + '/')
                for cp in comp_paths)
            if inside_comp:
                continue

            result.append((
                dat_path,
                self._cellVal(i, 'rel_file_path'),
                self._cellVal(i, 'type'),
                strategy,
            ))

        # Sort by path depth -- shallowest first
        result.sort(key=lambda x: x[0].count('/'))
        return result

    def _logDATRestorationReport(self, total, restored, errors) -> None:
        """Log a summary report after DAT restoration."""
        if errors:
            self.Log(
                f'DAT restoration complete: {restored}/{total} DAT(s) '
                f'restored, {errors} error(s)',
                'WARNING')
        else:
            self.Log(
                f'DAT restoration complete: {restored} DAT(s) restored '
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

    # Directories that must never be touched by empty-dir cleanup
    _SCM_DIRS = {'.git', '.svn', '.hg'}

    def deleteEmptyDirectories(self, path: Union[str, Path]) -> None:
        """
        Recursively delete empty directories only.
        SAFETY: rmdir() only succeeds on empty directories.
        Skips version-control directories (.git, .svn, .hg).
        Never operates on project.folder or its parents.
        """
        path = Path(path)
        if not path.is_dir():
            return

        # SAFETY: Never walk project.folder -- too broad, can delete
        # unrelated empty directories (e.g. newly-created target folders)
        try:
            if path.resolve() == Path(project.folder).resolve():
                return
        except Exception:
            pass

        empty_dir_found = True
        iteration = 0

        while empty_dir_found and iteration < 10:
            empty_dir_found = False
            iteration += 1

            for root, dirs, files in os.walk(str(path), topdown=False):
                # Skip version-control internals entirely
                if any(part in self._SCM_DIRS for part in Path(root).parts):
                    continue
                for dir_name in dirs:
                    if dir_name in self._SCM_DIRS:
                        continue
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

        For TOX-strategy COMPs, checks live oper.dirty (TD's native dirty flag
        updates immediately when a COMP is modified, before the next Refresh),
        falling back to the cached 'Par' table value for parameter changes.

        For TDN-strategy COMPs, oper.dirty is ALWAYS True (their externaltox is
        empty), so it is meaningless -- the fingerprint-derived 'dirty' value
        maintained in the table by dirtyHandler is authoritative. Using
        oper.dirty here counted every clean TDN COMP as dirty.

        For DATs and missing operators, uses the cached table value.
        """
        if self._performMode:
            return 0
        table = self.Externalizations
        if not table:
            return 0
        count = 0
        for i in range(1, table.numRows):
            op_path = str(self._cellVal(i, 'path'))
            oper = op(op_path)
            val = str(self._cellVal(i, 'dirty'))
            if oper and oper.valid and oper.family == 'COMP':
                # TDN COMPs: oper.dirty is always True -- trust the table.
                if self._cellVal(i, 'strategy') == 'tdn':
                    if val and val not in ('', 'False', 'Clean', 'Saved'):
                        count += 1
                    continue
                # TOX COMPs: TD's native dirty flag is immediate; the table
                # carries 'Par' for parameter-only changes between Refreshes.
                if oper.dirty or val == 'Par':
                    count += 1
                continue
            # For DATs or missing operators, use cached table value
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
                # explorer.exe /select,<path> returns exit code 1 even on
                # success (by design -- the launcher detaches). Don't gate
                # on the return code or every successful click logs a
                # false-positive warning.
                filepath = filepath.replace('/', '\\')
                subprocess.Popen(['explorer', f'/select,{filepath}'])
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
            {project.folder}/embody/base1.tdn -> /embody/base1

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
        """Capture the AUTHORED state of a COMP's parameters.

        Captures the authored value per mode (expr for EXPRESSION, bindExpr
        for BIND, val for CONSTANT) -- never par.eval(). This mirrors exactly
        what an externalized .tox/.tdn serializes: the authored parameter
        state, not its evaluated result. Using .eval() here was a bug -- a
        parameter bound to a time-varying expression (absTime.frame, an audio
        level, a moving CHOP) evaluated to a different value every Refresh,
        so compareParameters() reported the COMP dirty every cycle and
        triggered a redundant re-export, even though the on-disk file (which
        stores the expression text) was byte-identical. Reading authored
        values also avoids cook side effects and never raises on a broken
        expression. Matches EmbodyExt._parFingerprint.
        """
        params = {}
        for page in comp.pages + comp.customPages:
            for par in page.pars:
                if par.name in ['externaltox', 'file']:
                    continue
                mode = par.mode
                if mode == ParMode.EXPRESSION:
                    value = par.expr
                elif mode == ParMode.BIND:
                    value = par.bindExpr
                else:
                    value = par.val
                params[par.name] = {
                    'value': value,
                    'expr': par.expr if par.expr else None,
                    'bindExpr': par.bindExpr if par.bindExpr else None,
                    'mode': mode
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