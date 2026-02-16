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

    # ==========================================================================
    # INITIALIZATION
    # ==========================================================================

    def __init__(self, ownerComp: COMP) -> None:
        self.my = ownerComp
        self.lister = self.my.op('list/treeLister')
        self.tagging_menu_window = self.my.op('window_tagging_menu')
        self.tagger = self.my.op('tagger')
        self.root = op('/')
        
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
            'text': 'Txttag',
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
            'python': 'Pytag', 'tscript': 'Pytag', 'text': 'Txttag'
        }

        # Parameter tracker for detecting COMP changes
        self.param_tracker = ParameterTracker(self.my)

        # Set up Python environment (uv + venv) for Claudius dependencies
        # Only install if Claudius is enabled (user opted in during Verify)
        if self.my.par.Claudiusenable.eval():
            self._setupEnvironment()

    # ==========================================================================
    # PYTHON ENVIRONMENT SETUP (uv)
    # ==========================================================================

    def _setupEnvironment(self):
        """
        Set up a Python virtual environment using uv for Claudius dependencies.
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

        # Dependencies — pywin32 is Windows-only
        deps = ['mcp>=1.2.0']
        if sys.platform.startswith('win'):
            deps.append('pywin32>=306')

        # Fast path: if deps already installed, just add to sys.path
        if os.path.isdir(os.path.join(site_packages, 'mcp')):
            self._addSitePackages(site_packages)
            if sys.platform.startswith('win'):
                self._fixPywin32Dlls(site_packages)
            return

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
        self.Log('uv not found — installing via pip...')
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

        self.Log('Could not find uv after install — is Python user Scripts on PATH?', 'ERROR')
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
    # CLAUDIUS ONBOARDING
    # ==========================================================================

    def _promptClaudius(self):
        """Prompt user to enable Claudius (Claude Code integration)."""
        choice = ui.messageBox('Embody - Claude Code Integration',
            'Enable Claudius?\n\n'
            'Claudius is an MCP server that lets Claude Code\n'
            'create, modify, and query TouchDesigner operators.\n\n'
            'This will:\n'
            '  - Install Python dependencies (~30 MB)\n'
            '  - Start a local MCP server on port '
            f'{self.my.par.Claudiusport.eval()}\n'
            '  - Create CLAUDE.md and .mcp.json in your project\n\n'
            'Requires Claude Code CLI or VS Code extension.\n'
            'You can change this later via the Claudiusenable parameter.',
            buttons=['Skip', 'Enable Claudius'])

        if choice == 1:
            self._enableClaudius()
        else:
            self.my.par.Claudiusenable = False
            self.Log('Claudius skipped. Enable later via Claudiusenable parameter.', 'INFO')

    def _enableClaudius(self):
        """Enable Claudius: install deps, extract CLAUDE.md, start server."""
        self.Log('Setting up Claudius...', 'INFO')

        # Install Python dependencies
        self._setupEnvironment()

        # Extract CLAUDE.md to project/repo root
        self._extractClaudeMd()

        # Enable Claudius (triggers Start() via parexec.py)
        self.my.par.Claudiusenable = True

        self.Log(
            'Claudius enabled! If you have not installed Claude Code yet, visit:\n'
            '    https://docs.anthropic.com/en/docs/claude-code/overview',
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

    def _extractClaudeMd(self):
        """Extract the embedded CLAUDE.md template to the project/repo root."""
        template_dat = self.my.op('text_claude_md')
        if not template_dat:
            self.Log('CLAUDE.md template DAT not found inside Embody', 'WARNING')
            return None

        content = template_dat.text
        if not content:
            self.Log('CLAUDE.md template DAT is empty', 'WARNING')
            return None

        target_dir = self._findProjectRoot()
        claude_md_path = target_dir / 'CLAUDE.md'

        if claude_md_path.exists():
            existing = claude_md_path.read_text(encoding='utf-8')
            if '<!-- Generated by Embody/Claudius' in existing:
                # Our file -- safe to update
                claude_md_path.write_text(content, encoding='utf-8')
                self.Log(f'Updated CLAUDE.md at {claude_md_path}', 'SUCCESS')
            else:
                # User's own file -- write to CLAUDIUS.md instead
                fallback = target_dir / 'CLAUDIUS.md'
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

    def _upgradeClaudius(self):
        """Silently extract CLAUDE.md if Claudius is enabled but file is missing."""
        if not self.my.par.Claudiusenable.eval():
            return
        target_dir = self._findProjectRoot()
        if not (target_dir / 'CLAUDE.md').exists() and not (target_dir / 'CLAUDIUS.md').exists():
            self._extractClaudeMd()

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
                'path', 'type', 'rel_file_path', 'timestamp', 
                'dirty', 'build', 'touch_build'
            ])
            externalizations_dat.tags = [self.my.par.Tsvtag.eval()]
            self.Log(f"Created '{table_name}' tableDAT", "SUCCESS")
        else:
            externalizations_dat.clear(keepFirstRow=True)
            self.Log(f"Reset '{table_name}' tableDAT", "INFO")
        
        self.my.par.Externalizations.val = externalizations_dat

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

        # Stage 2: Claudius opt-in (deferred so Reset() completes first)
        run(f"op('{self.my}').ext.Embody._promptClaudius()", delayFrames=5)

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

        # Clear externalizations table
        if self.Externalizations:
            ext_path = str(self.Externalizations)
            run(lambda: op(ext_path).clear(keepFirstRow=True) if op(ext_path) else None, delayFrames=10)

        self.my.par.Status = 'Disabled'
        self.updateEnableButtonLabel('Enable')
        
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
                        self.Log(f"Removed empty directory: {comp_path}", "INFO")
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
                    self.Log(f"Removed empty directory: {folder}", "INFO")
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
        if choice:
            self.Disable(self.ExternalizationsFolder, choice - 1)

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

    def Update(self) -> None:
        """Main update method - process additions, subtractions, and dirty ops."""
        if self.my.par.Status != 'Enabled':
            return

        # Check for parameter changes
        for comp in self.getExternalizedOps(COMP):
            if self.param_tracker.compareParameters(comp):
                self.Externalizations[comp.path, 'dirty'] = 'Par'
                self.Save(comp.path)

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
        
        subtractions = [
            oper for oper in externalized_ops
            if not set(all_tags).intersection(oper.tags)
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

        # Handle dirty COMPs
        dirties = self.dirtyHandler(True)

        # Report results
        self._reportResults(dirties, additions, subtractions)
        run(f"op('{self.my}').par.Refresh.pulse()", delayFrames=1)
        self.updateEnableButtonLabel('Update')

    def _reportResults(self, dirties, additions, subtractions):
        """Report update results to log."""
        plural = any(len(lst) > 1 for lst in [dirties, additions, subtractions])
        if dirties:
            self.Log(f"Saved {dirties} tox{'es' if plural else ''}", "SUCCESS")
        if additions:
            self.Log(f"Added {len(additions)} operator{'s' if plural else ''} in total", "SUCCESS")
        if subtractions:
            self.Log(f"Removed {len(subtractions)} operator{'s' if plural else ''} in total", "SUCCESS")

    def Refresh(self) -> None:
        """Refresh Embody state and UI."""
        self.cleanupAllDuplicateRows()
        self.updateDirtyStates(self.ExternalizationsFolder)
        self.lister.par.Refresh.pulse()
        self.checkOpsForContinuity(self.ExternalizationsFolder)
        
        if self.my.par.Detectduplicatepaths:
            self.checkForDuplicates()
        
        self.Log("Refreshed", "INFO")
        
        if not me.time.play:
            self.Log("ALERT! TIMELINE IS PAUSED. RESUME FOR EMBODY TO FUNCTION", "ERROR")

    # ==========================================================================
    # OPERATOR QUERIES
    # ==========================================================================

    def getTags(self, selection: Optional[str] = None) -> list[str]:
        """Get all Embody tags, optionally filtered by type."""
        tags = [par.val for par in self.my.pars('*tag')]
        if selection == 'tox':
            return [t for t in tags if t == self.my.par.Toxtag.val]
        elif selection == 'DAT':
            return [t for t in tags if t != self.my.par.Toxtag.val]
        return tags

    def getExternalizedOps(self, opFamily: type) -> list[OP]:
        """Get all externalized operators of a given family from the table."""
        if not self.Externalizations:
            return []
            
        family_str = 'COMP' if opFamily == COMP else 'DAT'
        ops = []
        
        for i in range(1, self.Externalizations.numRows):
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
        tags = self.getTags('tox' if opFamily == COMP else 'DAT')
        return self.root.findChildren(
            type=opFamily,
            tags=tags,
            parName='externaltox' if opFamily == COMP else 'file',
            key=lambda x: (
                self.isOpEligibleToBeExternalized(x) and
                not x.path.startswith('/local/') and
                x.path != '/local' and
                x.type != 'engine'
            )
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
        """Save an externalized operator and update tracking."""
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

            self.Log(f"Saved {opPath}", "SUCCESS")
        except Exception as e:
            self.Log("Save failed", "ERROR", str(e))

        #self.Refresh()

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
        
        for i in range(1, self.Externalizations.numRows):
            if self.Externalizations[i, 'type'].val == 'tdn':
                continue
            if self.Externalizations[i, 'path'].val == comp_path:
                self.Save(comp_path)
                return

        # Check if any parent is externalized
        parent_comp = current_comp.parent()
        while parent_comp:
            for i in range(1, self.Externalizations.numRows):
                if self.Externalizations[i, 'type'].val == 'tdn':
                    continue
                if self.Externalizations[i, 'path'].val == parent_comp.path:
                    self.Save(parent_comp.path)
                    return
            parent_comp = parent_comp.parent()
        
        self.Log(f"No externalized COMP found at or above '{comp_path}'", "WARNING")

    def dirtyHandler(self, update: bool) -> list[str]:
        """Check and optionally update dirty COMPs."""
        updates = []
        for oper in self.getExternalizedOps(COMP):
            dirty = oper.dirty
            try:
                self.Externalizations[oper.path, 'dirty'] = dirty
            except Exception as e:
                self.Log(f"Failed to update dirty state for {oper.path}: {e}", "DEBUG")
                pass

            if dirty and update:
                self.Save(oper.path)
                updates.append(oper.path)

        return updates

    def updateDirtyStates(self, externalizationsFolder: str) -> None:
        """Update dirty states and check for path/parameter changes."""
        dirties = self.dirtyHandler(False)
        param_changes = []

        for oper in self.getExternalizedOps(COMP) + self.getExternalizedOps(DAT):
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

        if oper.family == 'COMP':
            self._setupCompForExternalization(oper, rel_file_path, save_file_path)
            dirty = oper.dirty
            build_num = int(oper.par.Build.eval()) if hasattr(oper.par, 'Build') else 1
            touch_build = str(oper.par.Touchbuild.eval()) if hasattr(oper.par, 'Touchbuild') else app.build
            self.param_tracker.updateParamStore(oper)
        else:  # DAT
            self._setupDatForExternalization(oper, rel_file_path, save_file_path)

        # Add to table
        self._addToTable(oper, rel_file_path, timestamp, dirty, build_num, touch_build)
        self.Log(f"Added '{oper.path}'", "SUCCESS")

    def _setupCompForExternalization(self, oper, rel_file_path, save_file_path):
        """Configure a COMP for externalization."""
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

    def _addToTable(self, oper, rel_file_path, timestamp, dirty, build_num, touch_build):
        """Add or update operator entry in externalizations table."""
        normalized_path = self.normalizePath(rel_file_path)
        
        # Check if row exists
        for row in range(1, self.Externalizations.numRows):
            if self.Externalizations[row, 'path'] == oper.path:
                self.Externalizations[row, 'rel_file_path'] = normalized_path
                return
        
        # Add new row
        self.Externalizations.appendRow([
            oper.path, oper.type, normalized_path, timestamp, 
            dirty, build_num, touch_build
        ])

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
            for i in range(1, self.Externalizations.numRows):
                row_path = self.Externalizations[i, 'path'].val
                if row_path:
                    rel_file_path = self.normalizePath(self.Externalizations[i, 'rel_file_path'].val)
                    rows_to_check.append((row_path, rel_file_path))

            processed_ops = set()

            for old_op_path, rel_file_path in rows_to_check:
                if old_op_path in processed_ops:
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

    def _handleMissingOperator(self, old_op_path, old_rel_file_path):
        """Handle an operator that no longer exists."""
        self.cleanupDuplicateRows(old_op_path)
        
        # Truly missing - remove from table
        self.Log(f"Operator '{old_op_path}' no longer exists!", "WARNING")
        for i in range(1, self.Externalizations.numRows):
            if self.Externalizations[i, 'path'].val == old_op_path:
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
            self.Externalizations.cook(force=True)
            self.cleanupDuplicateRows(new_op.path)
            
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
                        self.Log(f"Removed empty directory: {old_folder}", "INFO")
                        
                        current_dir = old_folder.parent
                        while current_dir.exists() and current_dir != Path(project.folder):
                            if not any(current_dir.iterdir()):
                                current_dir.rmdir()
                                self.Log(f"Removed empty parent: {current_dir}", "INFO")
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
        """Remove duplicate rows for a path, keeping most recent."""
        row_indices = []
        timestamps = []
        
        for i in range(1, self.Externalizations.numRows):
            if self.Externalizations[i, 'path'].val == path:
                row_indices.append(i)
                try:
                    ts_str = self.Externalizations[i, 'timestamp'].val
                    timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC") if ts_str else datetime.min
                except (ValueError, TypeError) as e:
                    self.Log(f"Failed to parse timestamp for row {i}: {e}", "DEBUG")
                    timestamp = datetime.min
                timestamps.append(timestamp)
        
        if len(row_indices) <= 1:
            return row_indices[0] if row_indices else None
        
        most_recent = timestamps.index(max(timestamps))
        row_to_keep = row_indices[most_recent]
        
        for i in sorted(row_indices, reverse=True):
            if i != row_to_keep:
                self.Externalizations.deleteRow(i)
                self.Log(f"Removed duplicate row {i} for {path}", "INFO")
        
        return row_to_keep

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
        if oper.type == 'engine':
            ui.messageBox('Embody Error', f"'{oper.type}' type not supported.", buttons=['Ok'])
            return

        if self.isReplicant(oper) or self.isClone(oper) or self.isInsideClone(oper):
            ui.messageBox('Embody Warning', 
                f"'{oper.path}' is a replicant or clone and cannot be externalized.", 
                buttons=['Ok'])
            return

        # Determine which tags to show
        if oper.type in self.supported_dat_types:
            switch.par.index = 1
        elif oper.family == 'COMP':
            switch.par.index = 2
        else:
            ui.messageBox('Embody Error', 
                'Tags can only be applied to COMPs or supported DATs.', 
                buttons=['Ok'])
            return

        run(lambda: self.SetupTagger(oper), delayFrames=1)
        run(f"op('{self.tagging_menu_window}').par.winopen.pulse()", delayFrames=2)

    def SetupTagger(self, oper: OP) -> None:
        """Configure tagger button colors based on operator tags."""
        params = self.tagger.op('tags')
        
        for i in range(1, params.numRows):
            button = self.tagger.op(f'button{i}')
            if button:
                # Reset color
                button.par.colorr = self.my.par.Taggingmenucolorr
                button.par.colorg.expr = self._alternateColor('parent.Embody.par.Taggingmenucolorg')
                button.par.colorb = self.my.par.Taggingmenucolorb
                
                # Highlight active tags
                tag_value = params[i, 'value'].val
                if tag_value in oper.tags:
                    if oper.family == 'COMP':
                        button.par.colorr = self.my.par.Toxtagcolorr
                        button.par.colorg.expr = self._alternateColor('parent.Embody.par.Toxtagcolorg')
                        button.par.colorb = self.my.par.Toxtagcolorb
                    else:
                        button.par.colorr = self.my.par.Dattagcolorr
                        button.par.colorg.expr = self._alternateColor('parent.Embody.par.Dattagcolorg')
                        button.par.colorb = self.my.par.Dattagcolorb

    def _alternateColor(self, color_ref):
        """Generate alternating color expression."""
        return f'{color_ref} if me.digits % 2 else {color_ref} - 0.05'

    def TagSetter(self, oper: OP, tag: str) -> bool:
        """Toggle a tag on an operator."""
        color = self._getTagColor(oper, tag)
        if color is None:
            return False

        if tag not in oper.tags:
            oper.tags.add(tag)
            oper.color = color
        else:
            oper.tags.remove(tag)
            self.resetOpColor(oper)
            rel_file_path = self.getExternalPath(oper)
            self.RemoveListerRow(oper.path, rel_file_path)
            
            if oper.family == 'COMP':
                oper.par.externaltox = ''
                oper.par.externaltox.readOnly = False
            elif oper.family == 'DAT':
                oper.par.file = ''
                oper.par.file.readOnly = False
        
        return True

    def _getTagColor(self, oper, tag):
        """Get appropriate color for tag on operator, or None if invalid."""
        if oper.family == 'COMP':
            if tag == self.my.par.Toxtag.val:
                return (self.my.par.Toxtagcolorr, self.my.par.Toxtagcolorg, self.my.par.Toxtagcolorb)
            self.Log("TOX tags can only be applied to COMPs", "ERROR")
            return None
        elif oper.family == 'DAT':
            if tag in self.getTags('DAT') and oper.type in self.supported_dat_types:
                return (self.my.par.Dattagcolorr, self.my.par.Dattagcolorg, self.my.par.Dattagcolorb)
            self.Log("DAT tags can only be applied to supported DAT types", "ERROR")
            return None
        
        self.Log("Tags can only be applied to COMPs or DATs", "ERROR")
        return None

    def applyTagToOperator(self, oper: OP, tag: str) -> bool:
        """Apply a tag to an operator."""
        color = self._getTagColor(oper, tag)
        if color is None:
            return False

        if tag not in oper.tags:
            oper.tags.add(tag)
            oper.color = color
            self.Log(f"Tag '{tag}' applied to '{oper.path}'", "SUCCESS")
            
            if oper.family == 'COMP' and oper.par.externaltox.eval():
                rel_file_path = self.normalizePath(oper.par.externaltox.eval())
                timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                self.Externalizations.appendRow([oper.path, oper.type, rel_file_path, timestamp, oper.dirty])
                self.Log(f"Added existing externalization to table", "SUCCESS")
        
        return True

    def TagExiter(self) -> None:
        """Close tagging menu."""
        self.tagging_menu_window.par.winclose.pulse()

    # ==========================================================================
    # PROJECT-WIDE EXTERNALIZATION
    # ==========================================================================

    def ExternalizeProject(self) -> None:
        """Externalize all compatible COMPs and DATs in project."""
        choice = ui.messageBox('Embody',
            'Add all compatible COMPs and DATs to Embody?\n'
            '(Palette components, clones, and replicants will be ignored)',
            buttons=['Cancel', 'Confirm'])
        
        if choice == 0:
            return

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
                tag_param = self.dat_type_to_tag.get(oper.type, 'Pytag')
                
                if oper.type == 'text':
                    ext = oper.par.extension.eval() if hasattr(oper.par, 'extension') else ''
                    lang = oper.par.language.eval() if hasattr(oper.par, 'language') else ''
                    tag_param = self.extension_to_tag.get(lang) or self.extension_to_tag.get(ext) or tag_param
                
                tag_value = getattr(self.my.par, tag_param).eval()
                self.applyTagToOperator(oper, tag_value)

        # Process COMPs
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
                        self.Log(f"Removed tracked file: {normalized_path}", "INFO")
                        
                        # Clean up empty parent directories
                        parent_dir = full_path.parent
                        while parent_dir.exists() and parent_dir != Path(project.folder):
                            try:
                                if not any(parent_dir.iterdir()):
                                    parent_dir.rmdir()
                                    self.Log(f"Removed empty directory: {parent_dir}", "INFO")
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

        # Remove from table (if row exists)
        try:
            self.Externalizations.deleteRow(op_path)
            self.Log(f"Removed '{op_path}' from table", "SUCCESS")
        except Exception as e:
            if 'Index invalid or out of range' in str(e):
                self.Debug(f"No table row for '{op_path}' — already removed or never added")
            else:
                self.Log(f"Error removing from table", "ERROR", str(e))

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

    def Manager(self, action: str) -> None:
        """Open or close the manager window."""
        win = self.my.op('window_manager')
        if action == 'open':
            win.par.winopen.pulse()
            self.Refresh()
        elif action == 'close':
            win.par.winclose.pulse()

    def updateEnableButtonLabel(self, label: str) -> None:
        """Update enable button label."""
        button = self.my.op('toolbar/container_left/initialize')
        button.par.Buttonofflabel = label
        button.par.Buttononlabel = label

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
            if hasattr(ext, 'my'):
                caller_info += f"@{ext.my.name}"
        elif 'me' in caller_locals:
            caller_info = f"{caller_locals['me'].path}"
        else:
            frame_info = inspect.getframeinfo(frame)
            caller_info = f"{os.path.basename(frame_info.filename)}:{frame_info.lineno}"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_frame = absTime.frame

        # Structured log entry string
        log_entry = f"{timestamp} | {current_frame:8} | {level:7} | [{caller_info:30}] | {message}"
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

        # Append structured entry to ring buffer for MCP access
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