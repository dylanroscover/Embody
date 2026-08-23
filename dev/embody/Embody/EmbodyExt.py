"""
Embody -- version control for TouchDesigner projects.

Core extension on the Embody COMP. Externalizes tagged COMPs and DATs to
diffable files (.tox / .tdn / .py / .json / ...) on every save, restores
them on project open, and keeps the tracking table (externalizations.tsv),
git integration, self-updater, setup wizard, and catalogs in sync.

Siblings on this COMP: EnvoyExt (MCP server), TDNExt (.tdn format),
CatalogManagerExt. Child COMPs host ConvoyExt (LAN relay) and UpdaterExt --
separate COMPs so their reinit doesn't restart the MCP server.

Files on disk are the source of truth; the .toe is recoverable from them.

Author: Dylan Roscover
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import shutil
import inspect
import json
import textwrap
from collections import deque
from datetime import datetime
from pathlib import Path
from glob import glob
from typing import Optional, Union, Any

# TD is a GUI process on Windows and owns no console, so every console child
# (git, uv, pip, python) gets a NEW console window -- a flash over the user's
# TD. CREATE_NO_WINDOW suppresses it; absent off-Windows, hence getattr.
# EVERY subprocess spawned from inside TD must pass creationflags=NO_WINDOW.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


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
        'text_rule_performance':             'performance',
        'text_rule_td_connectivity':         'td-connectivity',
        'text_rule_multi_session':           'multi-session',
        'text_rule_worktree_td_safety':      'worktree-td-safety',
    }

    # Skill DAT name -> slug (Claude Code only)
    _TEMPLATE_MAP_SKILLS = {
        'text_skill_create_operator':     'create-operator',
        'text_skill_debug_operator':      'debug-operator',
        'text_skill_externalize':         'externalize-operator',
        'text_skill_create_extension':    'create-extension',
        'text_skill_manage_annotations':  'manage-annotations',
        'text_skill_td_api_reference':    'td-api-reference',
        'text_skill_movie_export':        'movie-export',
        'text_skill_parameter_design':    'parameter-design',
        'text_skill_td_recovery':         'td-recovery',
        'text_skill_multi_session_etiquette': 'multi-session-etiquette',
        'text_skill_mcp_tools_reference': 'mcp-tools-reference',
        'text_skill_pop_networks':        'pop-networks',
        'text_skill_visual_aesthetics':   'visual-aesthetics',
        'text_skill_brief':               'brief',
        'text_skill_merge_divergent_tox': 'merge-divergent-tox',
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
        'Embodymode', 'Autoexternalize',
        # Dropped-.tox expression handling: an "Always Clean/Ignore" answer
        # must survive into the next session -- especially untitled projects
        # spawned from a default startup file, which re-load baked .toe
        # defaults every time and re-prompted forever (issue #60).
        'Toxdropexpr',
        # TDN
        'Tdnmode',
        'Embeddatsintdns', 'Embedstorageintdns', 'Tdndatsafety',
        'Tdncascade', 'Tdncreateonstart', 'Tdnstriponsave',
        'Toxrestoreonstart', 'Datrestoreonstart', 'Filecleanup',
        # Clipboard auto-paste watcher consent (TDN page). Persisting the
        # user's own choice is what makes the release-export scrub of this
        # par (see _TRANSIENT_STATUS_PARS) cost them nothing: a deliberate
        # Off is restored from config.json, while fresh installs get the
        # authored default (On).
        'Clipboardautopaste',
        # Self-update consent (Advanced page) -- must persist or the user's
        # opt-in dies in the very update it triggers (the replacement COMP
        # restores prefs from config.json).
        'Autoupdate',
        # Convoy consent (Convoy page). The canonical gate must survive a
        # restart or the user re-opts-in every launch; the convoy IDENTITY
        # is not here (it lives in the tracked .embody/project.json), and
        # the Phase 3+ dangerous gates deliberately stay OUT of this
        # whitelist per A-49.
        'Convoyenable', 'Convoyremotewake', 'Convoywakegrace',
        # Keyboard shortcuts (issue #50)
        'Enablekeyboardshortcuts',
        'Shortcutmanager', 'Shortcutupdateall', 'Shortcutupdatecomp',
        'Shortcutrefresh', 'Shortcutexportproject', 'Shortcutexportcomp',
        'Shortcutcopytdn', 'Shortcuttagger',
    })

    # Aiclient token -> how the Launchaiclient button opens it at the project
    # root (_findProjectRoot(), which honors Aiprojectroot).
    #   kind 'editor'   -> GUI editor opened with the root as its workspace
    #   kind 'terminal' -> new login-shell terminal at the root running the CLI
    # Editors resolve the REAL app/exe, never a PATH shim -- a `code` shim can be
    # hijacked (e.g. Cursor installs its own). CLIs run inside a real terminal so
    # its login shell rebuilds PATH (defeats the Dock-truncated-PATH problem where
    # a CLI in ~/.local/bin is invisible to a Dock-launched TD). Tokens absent
    # here (e.g. 'none') -> Launchaiclient logs "no launcher".
    _VSCODE_LAUNCH = {
        'kind': 'editor', 'app': 'Visual Studio Code',
        'bundle': 'com.microsoft.VSCode',
        'mac_cli': '/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code',
        'win_exe': [
            r'%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe',
            r'%ProgramFiles%\Microsoft VS Code\Code.exe',
            r'%ProgramFiles(x86)%\Microsoft VS Code\Code.exe',
        ],
        'win_shim': 'code',
        'install': 'https://code.visualstudio.com/download  (macOS: brew install --cask visual-studio-code)',
    }
    # Terminal CLIs carry a per-OS install spec (dict) instead of a single
    # string: the missing-CLI terminal guard renders it as step-by-step
    # instructions with THE command to paste on its own line, correct for the
    # shell the guard runs in (cmd.exe on Windows, zsh on macOS). Keys:
    #   name    -> display name shown in the guard header
    #   mac/win -> the one command to copy/paste (official installer, per
    #              the tool's own docs; the win command must run in cmd.exe)
    #   mac_alt/win_alt -> labeled alternative -- a PURE pasteable command
    #   mac_alt_note/win_alt_note -> caveat rendered under the alternative
    #   note    -> prerequisite line shown under the command (e.g. Node.js)
    #   docs    -> official install docs URL
    # A plain-string 'install' is still accepted (legacy single-line hint).
    _AICLIENT_LAUNCH = {
        'claudecode': {'kind': 'terminal', 'cli': 'claude', 'install': {
            'name': 'Claude Code',
            'mac': 'curl -fsSL https://claude.ai/install.sh | bash',
            'mac_alt': 'brew install --cask claude-code',
            'win': 'curl -fsSL https://claude.ai/install.cmd -o install.cmd '
                   '&& install.cmd && del install.cmd',
            'win_alt': 'winget install Anthropic.ClaudeCode',
            'docs': 'https://code.claude.com/docs/en/setup',
        }},
        'opencode':   {'kind': 'terminal', 'cli': 'opencode', 'install': {
            'name': 'OpenCode',
            'mac': 'curl -fsSL https://opencode.ai/install | bash',
            'mac_alt': 'brew install anomalyco/tap/opencode',
            'win': 'choco install opencode',
            'win_alt': 'npm install -g opencode-ai',
            'win_alt_note': 'needs Node.js -- https://nodejs.org',
            'docs': 'https://opencode.ai/docs/',
        }},
        'codex':      {'kind': 'terminal', 'cli': 'codex', 'install': {
            'name': 'Codex CLI',
            'mac': 'curl -fsSL https://chatgpt.com/codex/install.sh | sh',
            'mac_alt': 'brew install --cask codex',
            'win': 'powershell -ExecutionPolicy ByPass -c '
                   '"irm https://chatgpt.com/codex/install.ps1 | iex"',
            'win_alt': 'npm install -g @openai/codex',
            'win_alt_note': 'needs Node.js -- https://nodejs.org',
            'docs': 'https://developers.openai.com/codex/cli',
        }},
        'gemini':     {'kind': 'terminal', 'cli': 'gemini', 'install': {
            'name': 'Gemini CLI',
            'mac': 'npm install -g @google/gemini-cli',
            'mac_alt': 'brew install gemini-cli',
            'win': 'npm install -g @google/gemini-cli',
            'note': 'needs Node.js 20 or newer -- https://nodejs.org',
            'docs': 'https://www.geminicli.com/docs/get-started/installation',
        }},
        'copilot':    _VSCODE_LAUNCH,   # Copilot lives inside VS Code
        'vscode':     _VSCODE_LAUNCH,   # VS Code uses the same launcher as Copilot
        'cursor': {
            'kind': 'editor', 'app': 'Cursor',
            'bundle': 'com.todesktop.230313mzl4w4u92',
            'mac_cli': '/Applications/Cursor.app/Contents/Resources/app/bin/cursor',
            'win_exe': [r'%LOCALAPPDATA%\Programs\cursor\Cursor.exe'],
            'win_shim': 'cursor',
            'install': 'https://cursor.com/download  (macOS: brew install --cask cursor)',
        },
        'windsurf': {
            'kind': 'editor', 'app': 'Windsurf',
            'bundle': 'com.exafunction.windsurf',
            'alt_names': ('Devin Desktop',),   # Windsurf rebrand
            'win_exe': [r'%LOCALAPPDATA%\Programs\Windsurf\Windsurf.exe'],
            'win_shim': 'windsurf',
            'install': 'https://windsurf.com/editor/download  (macOS: brew install --cask windsurf)',
        },
        # 'none' -> not present -> "no launcher" log.
    }

    # TouchDesigner injects env vars that BREAK other apps launched as a fresh
    # process: ELECTRON_RUN_AS_NODE=1 makes Electron editors (VS Code, Cursor,
    # Windsurf) run headless-as-Node and quit instantly ("dock icon bounces,
    # then closes"); LD_LIBRARY_PATH/DYLD_* and PYTHON* point into TD's bundle
    # and can mis-link or mis-route other tools. launch_env (embody_launch
    # module DAT) strips these so a launched app/terminal starts clean.
    _LAUNCH_ENV_STRIP = frozenset({
        'ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS',
        'LD_LIBRARY_PATH', 'LD_PRELOAD',
        'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH', 'DYLD_INSERT_LIBRARIES',
        'PYTHONHOME', 'PYTHONPATH', 'PYTHONEXECUTABLE',
        'PYTHONDONTWRITEBYTECODE', 'PYTHONNOUSERSITE', 'PYTHONSTARTUP',
        'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH',
        '__CFBundleIdentifier',
        # link_env sets this to the project venv; a launched terminal
        # inheriting it (plus the venv Scripts/ on PATH -- launch_env
        # strips that too) would silently point bare pip/python at the
        # MCP stack's venv, bypassing InstallPackages' refusals.
        'VIRTUAL_ENV',
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

        # Parameter-dialog page filter (the POPX pattern). Default shows
        # only Embody's custom pages; the Advanced-page 'Show Built-in
        # Pars' toggle (issue #77) unhides TD's Layout/Panel/Look/... pages
        # for users who need them (e.g. the Common page's Global OP
        # Shortcut). The built-in pages stay fully functional either way --
        # showCustomOnly is just a dialog filter. Applied in __init__ (not
        # baked authored state alone) so every deployed copy converges on
        # the user's chosen value after any extension init.
        self.my.showCustomOnly = not bool(self.my.par.Showbuiltinpars.eval())

        # Suppress TD ThreadManager's benign "fallback strategy" warning that
        # fires on every standalone EnqueueTask call (used by Envoy and TDN).
        import logging
        logging.getLogger('TDAppLogger.threadManager_logger').setLevel(logging.ERROR)

        self.lister = self.my.op('list/list1')
        self.tagging_menu_window = self.my.op('window_tagging_menu')
        self.tagger = self.my.op('tagger')
        self.root = op('/')
        self._tagger_mode = 'tag'  # 'tag' or 'manage'

        # --- Auto-save / crash checkpoint engine state ---
        # tdn boundaries touched (via MCP) since the last drain; drained one per
        # frame at idle by _autosaveDrain. _autosave_gen + the identity guard make
        # the settle-drain run()-loop survive reinit / collapse re-arms (watchdog
        # pattern). monotonic, NEVER absTime (which pauses/resets).
        self._pending_checkpoint_roots = set()
        self._last_checkpoint_activity = 0.0   # time.monotonic()
        self._autosave_gen = 0
        self._autosave_armed = False
        # Empty-overwrite guard memo: abs_path -> ((path, mtime_ns, size),
        # refused). Keeps a STABLE refusal to one parse + one WARNING
        # instead of re-parsing the file on every sweep (per-session;
        # reinit re-warms it harmlessly).
        self._empty_guard_cache = {}
        # An op ran that could have touched ANY tracked root (execute_python),
        # so the drain must discover which ones actually changed. A flag, not a
        # queued set: the sweep is deferred to the settle so a burst of agent
        # calls costs ONE sweep, not one per call.
        self._coarse_checkpoint_due = False

        # COMP paths the user answered plain-Ignore for in the dropped-.tox
        # dialog this session -- subsequent sweeps skip them instead of
        # re-prompting (issue #60). Session-scoped by design: resets on
        # extension reinit / project reload. 'Always Ignore' persists via
        # the Toxdropexpr parameter instead.
        self._toxdrop_ignored_session = set()

        # Release-hook re-entrancy latch lives in COMP storage, NOT an
        # instance attribute: ExportPortableTox's own strip phase can
        # reinit this extension mid-export when the Embody COMP is the
        # target, and storage keeps the latch SHARED between the stale
        # instance still running the export and the fresh instance a
        # nested hook call resolves. Cleared here so a crash mid-hook (or
        # a True value baked into a saved .toe) can never leave hooks
        # silently disabled. Trade-off: a reinit fired DURING a hook run
        # also clears an active latch -- accepted; it requires the hook
        # itself to touch extension sources.
        self.my.store('_release_hook_active', False)

        # Logging configuration
        self.header = 'Embody >'
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
        # (which is always True when externaltox is empty). Kept in
        # ownerComp storage, NOT an instance attribute -- see the
        # _tdn_fingerprints property (must survive extension reinit).

        # NOTE: _setupEnvironment() is NOT called here.
        # It runs inside EnvoyExt.Start(), which is invoked after init() and
        # _restoreSettings() have run. Calling it here (based on the baked
        # Envoyenable value) would bypass the opt-in prompt on fresh .tox drop.

        # An EXISTING healthy venv is wired at init though (2026-08-19):
        # pure sys/os state, no subprocess, no prompt -- user extensions
        # with module-level imports need the venv importable BEFORE Envoy
        # start (and with Envoy disabled entirely). Never blocks init.
        try:
            self._initPythonEnv()
        except Exception as e:
            # Loud on purpose: an AttributeError here usually means the
            # embody_pyenv module DAT is missing from the COMP -- a state
            # where Envoy's bootstrap will also fail (2026-08-19 review).
            self.Log(f'Python env init wiring failed: {e!r}. If this '
                     f'names embody_pyenv, the module DAT is missing '
                     f'from the Embody COMP.', 'WARNING')

    # ==========================================================================
    # PYTHON ENVIRONMENT SETUP (uv)
    # ==========================================================================

    # Bump MCP_MIN_VERSION when a new release is tested and verified. The
    # dependency pin is always ``mcp>=MCP_MIN_VERSION,<next-major``: SDK 2.0.0
    # (2026-07-28) removed mcp.server.fastmcp overnight and every fresh
    # unpinned install broke (issue #81), so a new SDK major is adopted only
    # by a deliberate port + this constant's bump -- never by the resolver.
    # Bumping it (or changing any dep below) re-stamps the spec, and
    # _environmentNeedsInstall then auto-upgrades every existing venv on its
    # next Start -- users never rebuild a venv by hand.
    MCP_MIN_VERSION = '2.0.0'

    # The venv machinery lives in mod.embody_pyenv (extracted 2026-08-19;
    # one module owns spec building, uv invocation, stamping, wiring, the
    # import gate, and the user-extras layer). The same-named facades below
    # keep every call site (ConvoyExt, embody_admin, tests) unchanged.
    # FACADES ARE MAIN-THREAD ONLY: mod.* is a TD object lookup. Worker
    # threads receive module functions pre-resolved on the main thread
    # (EnvoyExt._beginAsyncBootstrap pattern) -- never a facade.

    def _venvPaths(self) -> dict:
        """Compute venv / site-packages paths and the dependency list.

        Reads ``project.folder`` (a TouchDesigner global), so this MUST run on
        the main thread. The returned dict is plain data -- safe to hand to a
        worker thread (see _installDependencies), which is the whole point of
        separating it from the install work.
        """
        declared = []
        root = None
        try:
            # Rides on the spec so worker-side freeze_constraints can
            # exclude the user's own extras from the core snapshot.
            root = str(self._findProjectRoot())
            declared = mod.embody_pyenv.read_declared_extras(root)
        except Exception:
            pass
        # state_root: .embody/ state (the install lock) belongs at the
        # PROJECT ROOT like every other .embody consumer -- rooted at
        # project.folder it lands outside the managed gitignore when the
        # .toe sits in a repo subfolder (review find, 2026-08-19).
        return mod.embody_pyenv.venv_paths(
            project.folder, self.MCP_MIN_VERSION, declared_extras=declared,
            state_root=root)

    @staticmethod
    def _cryptographyPin(platform_name: str, machine: str) -> str:
        """The venv's cryptography pin for this target -- see embody_pyenv."""
        return mod.embody_pyenv.cryptography_pin(platform_name, machine)

    @staticmethod
    def _stampNeedsArchMigration(stamp_machine: str,
                                 platform_name: str) -> bool:
        """Pre-arch-stamp darwin migration verdict -- see embody_pyenv."""
        return mod.embody_pyenv.stamp_needs_arch_migration(
            stamp_machine, platform_name)

    @staticmethod
    def _measureVenvArch(venv_python) -> 'str | None':
        """Fresh-spawn architecture of the venv python -- see embody_pyenv."""
        return mod.embody_pyenv.measure_venv_arch(venv_python)

    @staticmethod
    def _venvPythonTag(venv_dir) -> 'str | None':
        """major.minor a venv was built for (pyvenv.cfg) -- see embody_pyenv."""
        return mod.embody_pyenv.venv_python_tag(venv_dir)

    def _environmentNeedsInstall(self, spec: Optional[dict] = None) -> bool:
        """Cheap, non-blocking check: does the venv need a (slow) install?

        Returns True when a venv build / pip install is required -- because the
        mcp package is absent, outside ``[MCP_MIN_VERSION, next-major)``,
        paired with an incompatible attrs 25.x, or because the venv was built
        for a DIFFERENT dependency spec or Python than this Embody wants (the
        ``embody-env.json`` stamp _installDependencies writes). The stamp is
        what carries every existing install forward on upgrade: any release
        that changes a pin makes older venvs report needs-install once, and
        the background bootstrap upgrades them in place -- nobody rebuilds a
        venv by hand (issue #81). Reads only the filesystem (versions come
        from ``*-X.Y.Z.dist-info`` directory names), so there is no
        subprocess, no network, and no import. Safe to call on the main
        thread before every Start() to decide sync-vs-async bootstrap.

        Side effect: a Python-version mismatch sets ``spec['recreate_venv']``
        so _installDependencies rebuilds the venv (``uv venv --clear``)
        instead of installing into one whose binary wheels target the old
        interpreter ABI.
        """
        spec = spec or self._venvPaths()
        return mod.embody_pyenv.environment_needs_install(spec)

    def _wirePythonPaths(self, spec: Optional[dict] = None, log=None) -> bool:
        """Wire the Envoy venv paths into this interpreter.

        Worker-thread safe when ``spec`` is provided: touches only os/sys state
        and never reads TD objects or logs. ``spec=None`` preserves the legacy
        main-thread convenience path by resolving _venvPaths() first.
        ``log`` names the failing precondition -- see wire_python_paths.
        """
        spec = spec or self._venvPaths()
        return mod.embody_pyenv.wire_python_paths(spec, log=log)

    @staticmethod
    def _importGateCheck(site_packages: 'str | None' = None) -> tuple[bool, str]:
        """Pure import gate for Envoy's MCP stack.

        Safe to call from a background thread: no TD objects, no logging, no
        parameter access. Returns ``(True, '')`` when ``mcp.server.mcpserver``
        -- the module EnvoyExt actually serves with -- imports. Gating on the
        exact module matters: SDK 2.0.0 kept ``mcp.server`` importable while
        REMOVING ``mcp.server.fastmcp``, so a parent-package gate passed and
        the server then died in a 30-minute retry storm (issue #81).

        When ``site_packages`` is provided, also refuses the stale-interpreter
        upgrade state: dependencies upgraded on disk while an older mcp stack
        is already imported in this process. Importing new-major submodules
        through cached old parents yields a mixed stack (and re-running
        pydantic model definitions over a live pydantic_core can abort() the
        process), so the only safe exit is a TD restart -- say so instead of
        trying.
        """
        return mod.embody_pyenv.import_gate_check(site_packages)

    @staticmethod
    def _mcpDistVersion(site_packages) -> 'str | None':
        """Newest parseable mcp dist-info version -- see embody_pyenv."""
        return mod.embody_pyenv.mcp_dist_version(site_packages)

    @staticmethod
    def _importGateFailureMessage(site_packages, message):
        return mod.embody_pyenv.import_gate_failure_message(
            site_packages, message)

    def _setupEnvironment(self):
        """
        Set up a Python virtual environment using uv for Envoy dependencies.
        Installs uv if not found, creates .venv, installs packages.
        Adds the venv's site-packages to sys.path so TD can import from it.

        Returns True if the environment is ready (mcp.server.mcpserver
        importable), False if any step failed. Callers (e.g. EnvoyExt.Start)
        MUST gate on this -- continuing past a False return produces an
        inscrutable 'No module named mcp.server.mcpserver' traceback at
        server-start time.

        Synchronous. The slow install and import gate run on the calling thread,
        so this is only safe when blocking is acceptable. EnvoyExt.Start()
        routes both the install-needed case and the first import gate through
        background threads instead -- see _installDependencies,
        _wirePythonPaths, _importGateCheck, and EnvoyExt._beginAsyncBootstrap.
        """
        spec = self._venvPaths()
        site_packages = spec['site_packages']
        venv_existed = os.path.isdir(spec['venv_dir'])  # only record a venv Embody creates

        if self._environmentNeedsInstall(spec):
            msgs = []
            ok = self._installDependencies(
                spec, log=lambda m, lvl='INFO': msgs.append((lvl, m)))
            for lvl, m in msgs:
                self.Log(m, lvl)
            if not ok:
                return False
            if not venv_existed and os.path.isdir(spec['venv_dir']):
                self._manifestRecordVenv(self._findProjectRoot(), spec['venv_dir'])

        if not self._wirePythonPaths(spec):
            self.Log(
                self._importGateFailureMessage(
                    site_packages, 'venv site-packages path is missing'),
                'ERROR',
            )
            return False
        # PATH/DLL/VIRTUAL_ENV linking is main-thread-only and separate
        # from sys.path wiring; a rebuild invalidates the retained DLL
        # handle first (Windows re-resolution across delete/recreate of
        # the same dir is not guaranteed).
        if spec.get('recreate_venv'):
            mod.embody_pyenv.unlink_dll_dir(spec['venv_dir'])
        self._linkEnv(spec)
        if sys.platform.startswith('win'):
            self._fixPywin32Dlls(site_packages)

        # A rebuild wiped user extras with the venv -- re-arm the
        # non-gating reconcile (the async path does this in
        # _pollBootstrap; this is the synchronous/wizard path's twin).
        self._scheduleExtrasApply(delay_frames=1)
        self._ensurePyEnvContext()

        # Opportunistic, non-blocking check for a newer mcp on PyPI.
        try:
            from importlib.metadata import version as pkg_version
            self._checkMCPUpdate(pkg_version('mcp'))
        except Exception:
            pass

        return self._verifyMcpImportable(site_packages)

    def _installDependencies(self, spec: dict, log) -> bool:
        """Build the venv and pip-install Envoy's CORE dependencies.

        MAIN-THREAD facade (the mod lookup): EnvoyExt's async bootstrap
        binds mod.embody_pyenv.install_dependencies on the main thread and
        hands THAT (worker-safe) function to its worker -- never this
        facade. Does NOT touch sys.path or import mcp -- callers do that
        separately so the delicate pydantic_core import can run off the TD
        main thread.
        """
        return mod.embody_pyenv.install_dependencies(spec, log)

    @staticmethod
    def _writeEnvStamp(spec: dict) -> None:
        """Record what the venv was built for -- see embody_pyenv."""
        mod.embody_pyenv.write_env_stamp(spec)

    def _verifyMcpImportable(self, site_packages):
        """Final gate: confirm mcp.server.mcpserver imports inside TD's process.

        A populated site-packages is necessary but not sufficient -- a partial
        install or load-time failure (missing native dep, etc.) would still
        leave the server unable to start. Catching it here yields a useful
        textport message instead of an inscrutable traceback at run time.

        Fast path: if mcp.server.mcpserver is already in sys.modules, a
        previous Start() in this session already imported it successfully --
        return True without touching sys.modules.  Tearing down and
        re-importing mcp.* on top of an already-loaded pydantic_core (Rust C
        extension) can panic the validator and abort() the process with no
        Python traceback -- the "TD just closes on Envoy toggle off/on" crash
        users hit on 5.0.393+.
        """
        ok, message = self._importGateCheck(site_packages)
        if ok:
            sys._envoy_import_gate_ok = True
            return True
        self.Log(self._importGateFailureMessage(site_packages, message), 'ERROR')
        return False

    @staticmethod
    def _bootstrapEnv(**extra) -> dict:
        """Environment for bootstrap children (python -m pip, uv).

        Starts from TD's environment (children may need TD's loader vars)
        but drops the variables that redirect Python's module search:
        PYTHONPATH, PYTHONSTARTUP, PYTHONUSERBASE, PYTHONNOUSERSITE, plus
        VIRTUAL_ENV (2026-08-19: tdPyEnvManager sets it process-wide to
        ITS env and uv honors it when --python is absent).
        TouchDesigner's 'Python 64-bit Module Path' preference reaches
        child processes through the environment on macOS, and users also
        set PYTHONPATH globally (the TD-documented alternative to the
        preference) -- either way a foreign site-packages leaking into
        the pip/uv children changes what they resolve and print (field,
        2026-08-18: it turned pip's output non-ASCII and killed env setup
        on a GUI-launched macOS TD whose default IO codec is US-ASCII).
        PYTHONIOENCODING pins child output to UTF-8 so the parent-side
        decode (forced utf-8) is byte-exact on every platform and locale.

        WORKER-THREAD SAFE: reads os.environ only.
        """
        return mod.embody_pyenv.bootstrap_env(**extra)

    @staticmethod
    def _resolveUv() -> 'str | None':
        """Locate an existing uv WITHOUT installing -- see embody_pyenv."""
        return mod.embody_pyenv.resolve_uv()

    def _findOrInstallUv(self, python_exe, log=None):
        """Find uv or install it via pip --user -- see embody_pyenv.
        MAIN-THREAD facade; workers get the module function pre-resolved."""
        return mod.embody_pyenv.find_or_install_uv(python_exe, log or self.Log)

    def _addSitePackages(self, site_packages):
        """Add venv site-packages to sys.path -- see embody_pyenv."""
        mod.embody_pyenv.add_site_packages(site_packages)

    def _fixPywin32Dlls(self, site_packages):
        """Copy pywin32 DLLs to win32/ -- see embody_pyenv."""
        mod.embody_pyenv.fix_pywin32_dlls(site_packages)

    # ------------------------------------------------------------------
    # Public Python-environment surface (2026-08-19). The project venv is
    # SHARED: any script in TD can import from it once wired, external
    # tools can run VenvPython, and declared extras travel in the
    # committed .embody/project.json (python.extras) -- surviving venv
    # rebuilds, TD upgrades, and machine moves. Extras never gate Envoy.
    # ------------------------------------------------------------------

    @property
    def VenvPython(self) -> str:
        """Absolute path to the project venv's python interpreter, or ''
        when the venv does not exist yet. For running external scripts
        against the project environment. macOS caveat: TD's signed python
        can refuse foreign NATIVE modules when spawned standalone (library
        validation) -- inside TD the same packages import fine."""
        spec = self._venvPaths()
        p = spec['venv_python']
        return p if os.path.isfile(p) else ''

    @property
    def VenvSitePackages(self) -> str:
        """Absolute path to the project venv's site-packages, or ''."""
        spec = self._venvPaths()
        p = spec['site_packages']
        return p if os.path.isdir(p) else ''

    def InstallPackages(self, packages, allow_shadow: bool = False) -> dict:
        """Add third-party Python packages to the shared project venv.

        Validates each requirement (plain name-based specs only; Embody's
        pinned core stack and TouchDesigner-bundled packages are refused
        -- shadowing TD's numpy/opencv is a crash class; override
        per-call with ``allow_shadow=True``, which is recorded in the
        declaration so the opt-in survives rebuilds). Accepted specs are
        written to the COMMITTED ``.embody/project.json`` under
        ``python.extras`` -- the declaration other machines see -- and
        acknowledged in the machine-local ``.embody/local.json`` (this
        machine's consent; a pulled declaration never installs without
        it). The install runs in the background, wheels-only,
        constrained by the frozen core closure. Non-blocking: returns
        {'accepted', 'refused', 'declared', 'status'} immediately;
        install results land in the Embody log, including a
        restart-required notice when a changed dist is already imported.
        To remove a package, delete it from python.extras (takes effect
        at the next venv rebuild) or uninstall it via VenvPython.
        """
        if isinstance(packages, str):
            packages = [packages]
        packages = [str(p).strip() for p in (packages or []) if str(p).strip()]
        pyenv = mod.embody_pyenv
        spec = self._venvPaths()
        accepted, refused = pyenv.check_extras_specs(
            packages, spec, allow_shadow)
        root = str(self._findProjectRoot())
        declared = pyenv.read_declared_extras(root)
        result = {'accepted': accepted, 'refused': refused,
                  'declared': declared, 'status': 'nothing to do'}
        for s, reason in refused.items():
            self.Log(f'InstallPackages refused {s!r}: {reason}', 'WARNING')
        if not accepted:
            return result
        # Merge keyed by DIST NAME, new spec superseding old: unioning
        # raw strings left 'requests>=2.28' AND 'requests==2.31' both
        # declared, feeding uv two constraints on one dist forever
        # (2026-08-19 review).
        by_name = {}
        for s in declared:
            by_name.setdefault(pyenv.spec_dist_name(s) or s, s)
        for s in accepted:
            by_name[pyenv.spec_dist_name(s) or s] = s
        merged = sorted(set(by_name.values()))
        shadow_names = ([pyenv.spec_dist_name(s) for s in accepted]
                        if allow_shadow else None)
        if not pyenv.write_declared_extras(root, merged, self.Log,
                                           allow_shadow_names=shadow_names):
            result['status'] = 'error: could not update .embody/project.json'
            return result
        result['declared'] = merged
        # This call IS this machine's consent -- record it so the
        # background reconcile may act on these specs. A failed consent
        # write must not report 'scheduled' (the reconcile would classify
        # the specs unacknowledged and install nothing).
        if not pyenv.acknowledge_extras(root, accepted, self.Log):
            result['status'] = ('error: could not record consent in '
                                '.embody/local.json -- retry')
            return result
        # A re-requested spec that failed before should retry NOW, not
        # stay suppressed behind its failure record.
        pyenv.clear_extras_failures(spec, accepted)
        if pyenv.environment_needs_install(spec):
            result['status'] = ('deferred: the Python environment is not '
                                'built yet -- extras install after the '
                                'core bootstrap (enable Envoy or run the '
                                'setup wizard)')
            self.Log(f'Extras declared ({", ".join(accepted)}) but the '
                     f'venv is not built yet -- they install after the '
                     f'core bootstrap.', 'WARNING')
            return result
        result['status'] = 'scheduled'
        self.Log(
            f'Extras declared ({", ".join(accepted)}) -- installing in '
            f'the background. Commit .embody/project.json so the '
            f'declaration travels with the project.', 'INFO')
        self._scheduleExtrasApply(delay_frames=1)
        return result

    def ApplyDeclaredExtras(self) -> dict:
        """Consent to and install EVERY package declared in
        .embody/project.json's python.extras on THIS machine.

        A declaration pulled from git never auto-installs (installing is
        code execution; consent is machine-local). This is the explicit
        yes: it acknowledges all currently-declared installable specs in
        .embody/local.json and kicks the background install. Returns the
        reconcile status (refused entries stay refused).
        """
        pyenv = mod.embody_pyenv
        root = str(self._findProjectRoot())
        declared = pyenv.read_declared_extras(root)
        spec = self._venvPaths()
        status = pyenv.extras_status(
            spec, declared, acknowledged=None,
            allow_shadow_names=pyenv.read_allow_shadow_names(root))
        installable = [s for s in declared
                       if s not in (status.get('refused') or {})]
        if installable:
            if not pyenv.acknowledge_extras(root, installable, self.Log):
                self.Log('Could not record consent in .embody/local.json '
                         '-- nothing installed; retry.', 'ERROR')
                return status
            self.Log(f'Acknowledged {len(installable)} declared extra(s) '
                     f'on this machine -- installing in the background.',
                     'INFO')
            self._scheduleExtrasApply(delay_frames=1)
        else:
            self.Log('No installable extras declared.', 'INFO')
        return status

    def _declaredExtras(self) -> list:
        """The committed extras declaration -- see embody_pyenv."""
        return mod.embody_pyenv.read_declared_extras(
            str(self._findProjectRoot()))

    def _linkEnv(self, spec: Optional[dict] = None) -> bool:
        """PATH/DLL/VIRTUAL_ENV linking -- MAIN THREAD ONLY facade."""
        return mod.embody_pyenv.link_env(spec or self._venvPaths())

    def _ensurePyEnvContext(self, force=False, startup=False):
        """Keep TD's pre-cook venv context current (mechanism: the
        embody_pyenv "TD pre-cook venv context authoring" section).
        Never touches a foreign context; a user-deleted file stays
        deleted (manifest tombstone) unless ``force`` (InitEnvoy);
        ``startup`` defers Advanced-mode consent, never a modal on open.
        MAIN THREAD ONLY, never raises."""
        if getattr(app, 'pyEnvHelper', None) is None:
            return  # pre-2025.32280 TD: no pre-cook channel
        try:
            self._ensurePyEnvContextInner(force, startup)
        except Exception as e:
            # Best-effort everywhere it is called from (extension init,
            # install epilogues, InitEnvoy) -- upkeep of this file must
            # never take down env setup or startup.
            self.Log(f'TD pre-cook venv context upkeep failed: {e}',
                     'WARNING')

    def _ensurePyEnvContextInner(self, force, startup):
        """The state machine behind _ensurePyEnvContext (which owns the
        docstring and the never-raise wrapper)."""
        pyenv = mod.embody_pyenv
        spec = self._venvPaths()
        project_dir = spec['project_dir']
        root = str(self._findProjectRoot())
        ctx_path = os.path.join(project_dir, pyenv.TD_CONTEXT_FILENAME)
        if (not os.path.isdir(spec['site_packages'])
                or pyenv.environment_needs_install(spec)):
            # TD must not pre-cook-link a venv Embody refuses to wire.
            # Un-record so the rewrite after the next successful install
            # is not read as a user deletion.
            if pyenv.remove_td_context_if_ours(project_dir,
                                               spec['venv_dir'],
                                               log=self.Log):
                self._manifestUnrecordCreatedFile(root, ctx_path)
                self.Log('Removed the TD pre-cook venv context -- the venv '
                         'needs (re)install. It returns after the next '
                         'successful install.', 'WARNING')
            return
        status = pyenv.td_context_status(
            project_dir, spec['venv_dir'], spec['python_tag'])
        if status == 'foreign':
            # detect_tdpyenvmanager owns routine messaging; an explicit
            # re-assert (InitEnvoy) deserves a direct answer.
            if force:
                self.Log(f'A TD env context Embody does not own sits at '
                         f'{project_dir} -- left untouched. Remove or '
                         f'repair it to hand the pre-cook link to Embody.',
                         'INFO')
            return
        if status == 'ok':
            return
        if status == 'absent' and not force:
            manifest = mod.embody_git.load_install_manifest(self, root)
            if (self._manifestRelPath(root, ctx_path)
                    in manifest.get('files_created', [])):
                return  # user deleted it -- InitEnvoy re-asserts

        def _write():
            # refresh: surgical pythonVersion update so TD/palette-written
            # keys (extraPaths, ...) survive a TD-python bump.
            (pyenv.refresh_td_context if status == 'refresh'
             else pyenv.write_td_context)(project_dir, spec['python_tag'])
            self._manifestRecordCreatedFile(root, ctx_path)
            # Anchored ignore entry ('/x' at root, 'dev/x' in a subdir) so
            # FOREIGN contexts elsewhere in the repo stay commit-able.
            rel = self._manifestRelPath(root, ctx_path)
            if not os.path.isabs(rel) and os.path.exists(
                    os.path.join(root, '.git')):
                entry = rel if '/' in rel else '/' + rel
                mod.embody_git.ensure_gitignore_entry(self, root, entry)
            self.Log(f'TD pre-cook venv context written: {ctx_path}')

        action = (f'{"refresh" if status == "refresh" else "write"} '
                  f'{pyenv.TD_CONTEXT_FILENAME} in {project_dir} '
                  f'(TouchDesigner pre-cook venv link)')
        details = [ctx_path,
                   f'{root}/.gitignore (anchored ignore entry, if missing)']
        prior = self._startup_config_pass
        self._startup_config_pass = prior or startup
        try:
            self._guardFileWrite('Python env', action, details, _write)
        finally:
            self._startup_config_pass = prior

    def _initPythonEnv(self):
        """Wire an existing healthy venv at extension init (2026-08-19).

        tdPyEnvManager links pre-cook and Embody historically waited for
        Envoy Start (frame 30+), so module-level imports in user
        extensions failed on cold open and worked on re-cook -- the worst
        bug shape. sys.path + PATH/DLL wiring only: no subprocess, no
        install, no prompt (the deliberate exclusion of _setupEnvironment
        from __init__ stands). Also runs the read-only tdPyEnvManager
        co-existence check once per session, adopts any extras install a
        replaced instance left running, and arms the non-gating extras
        reconcile.
        """
        spec = self._venvPaths()
        pyenv = mod.embody_pyenv
        if (os.path.isdir(spec['site_packages'])
                and not pyenv.environment_needs_install(spec)):
            pyenv.wire_python_paths(spec, log=self.Log)
            pyenv.link_env(spec)  # __init__ runs on the main thread
            sys._embody_pyenv_unwired_logged = False
        elif not getattr(sys, '_embody_pyenv_unwired_logged', False):
            # Nothing is wired at all -- the state a cold-open
            # ModuleNotFoundError actually comes from, and it was silent
            # (field 2026-08-20). Once per session: reinit is routine
            # here (source DATs hot-sync). The flag clears on a
            # successful wire, so a later break reports again.
            sys._embody_pyenv_unwired_logged = True
            self.Log(
                f'Python environment NOT wired: the project venv at '
                f'{spec["venv_dir"]} is missing or needs (re)install, so '
                f'nothing installed in it can import this session -- a '
                f'module-level import in an extension will fail. Enable '
                f'Envoy (or call op.Embody.InstallPackages) to build it.',
                'WARNING')
        if not getattr(sys, '_embody_pyenv_tdpem_checked', False):
            sys._embody_pyenv_tdpem_checked = True
            try:
                finding = pyenv.detect_tdpyenvmanager(
                    str(self._findProjectRoot()), spec['venv_dir'], app,
                    extra_dirs=[spec['project_dir']])
                notice = pyenv.tdpyenvmanager_notice(finding)
                if notice:
                    self.Log(notice[1], notice[0])
            except Exception:
                pass
        # TD's pre-cook context is ensured off the init path (after the
        # frame-30/45/60 restore phases) -- it only helps the NEXT launch,
        # and deferring keeps any Advanced-mode consent off frame 0.
        run('args[0]._ensurePyEnvContext(startup=True)', self,
            delayFrames=90)
        # A replaced instance may have a worker mid-install: adopt its
        # result instead of orphaning it (reinit is routine here --
        # source DATs hot-sync). The sys-level result slot makes the
        # hand-off instance-independent.
        if getattr(sys, '_embody_pyenv_installing', None) is not None:
            run('args[0]._pollExtrasInstall()', self, delayFrames=30)
            return
        # Reconcile declared extras well after the startup restore phases
        # (frames 30/45/60) so a fresh open never races them.
        self._scheduleExtrasApply(delay_frames=120)

    def _scheduleExtrasApply(self, delay_frames: int = 120) -> None:
        """Arm the non-gating extras reconcile; cheap no-op when nothing
        is declared, pending, acknowledged, or the venv needs its CORE
        install (the bootstrap owns the venv then and re-arms us after).
        Surfaces refused and unacknowledged declarations once per
        session -- never silently dropped. Pure JSON reads."""
        try:
            pyenv = mod.embody_pyenv
            root = str(self._findProjectRoot())
            declared = pyenv.read_declared_extras(root)
            if not declared:
                return
            if not getattr(sys, '_embody_pyenv_root_logged', False):
                sys._embody_pyenv_root_logged = True
                self.Log(f'Python extras declaration: '
                         f'{os.path.join(root, ".embody", "project.json")}',
                         'DEBUG')
            spec = self._venvPaths()
            if pyenv.environment_needs_install(spec):
                return
            status = pyenv.extras_status(
                spec, declared,
                acknowledged=pyenv.read_acknowledged_extras(root),
                allow_shadow_names=pyenv.read_allow_shadow_names(root))
            refused = status.get('refused') or {}
            if refused and (getattr(sys, '_embody_pyenv_refused_logged', None)
                            != set(refused)):
                sys._embody_pyenv_refused_logged = set(refused)
                applied_now = set(status.get('applied') or [])
                for s, reason in refused.items():
                    tail = ''
                    if s in applied_now:
                        # Refused-but-already-installed: warning alone
                        # leaves the package live on sys.path -- name the
                        # remediation (2026-08-19 review).
                        tail = (' NOTE: this package is ALREADY installed '
                                'and still active -- remove it with: uv '
                                'pip uninstall <name> --python '
                                '<op.Embody.VenvPython>.')
                    self.Log(f'Declared extra not installable: {reason}.'
                             f'{tail}', 'WARNING')
            unack = status.get('unacknowledged') or []
            if unack and (getattr(sys, '_embody_pyenv_unack_logged', None)
                          != set(unack)):
                sys._embody_pyenv_unack_logged = set(unack)
                self.Log(
                    f'{len(unack)} Python package(s) are declared in '
                    f'.embody/project.json but have not been approved on '
                    f'this machine: {", ".join(unack)}. Installing them '
                    f'runs third-party code -- review the declaration, '
                    f'then run op.Embody.ApplyDeclaredExtras() to '
                    f'install.', 'WARNING')
            exhausted = status.get('exhausted') or []
            if exhausted and (getattr(sys, '_embody_pyenv_exhausted_logged',
                                      None) != set(exhausted)):
                sys._embody_pyenv_exhausted_logged = set(exhausted)
                self.Log(
                    f'Gave up on {", ".join(exhausted)} after 3 failed '
                    f'attempts (transient errors each time). Re-request '
                    f'via op.Embody.InstallPackages() to try again.',
                    'WARNING')
            if not status['to_install']:
                return
            run('args[0]._applyDeclaredExtras()', self,
                delayFrames=max(1, int(delay_frames)))
        except Exception:
            pass

    def _applyDeclaredExtras(self) -> None:
        """Main-thread entry: spawn the worker installing declared extras.

        Everything TD-flavored (mod resolution, project root, spec) is
        resolved HERE; the worker receives plain data and a pure module
        function, and publishes its result to a SYS-LEVEL slot so a
        replaced instance's poll (or the next instance) can consume it --
        reinit is routine. The busy flag stores (monotonic, thread) and
        is stale only when its worker thread is dead: a wall-clock expiry
        alone started a second concurrent uv under any install longer
        than the window (2026-08-19 review). Cross-process safety is the
        module's O_EXCL install lock, not this flag."""
        try:
            if self.my.ext.Embody is not self:
                return  # stale instance; the fresh one re-arms at init
        except Exception:
            return
        busy = getattr(sys, '_embody_pyenv_installing', None)
        if busy:
            thread = busy.get('thread') if isinstance(busy, dict) else None
            if thread is not None and thread.is_alive():
                # Genuine install in flight -- check back rather than
                # dropping this request on the floor.
                run('args[0]._pollExtrasInstall()', self, delayFrames=60)
                return
            sys._embody_pyenv_installing = None  # dead worker: reclaim
        try:
            pyenv = mod.embody_pyenv  # MAIN-THREAD resolution
            root = str(self._findProjectRoot())
            declared = pyenv.read_declared_extras(root)
            spec = self._venvPaths()
            if pyenv.environment_needs_install(spec):
                return
            to_install = pyenv.extras_status(
                spec, declared,
                acknowledged=pyenv.read_acknowledged_extras(root),
                allow_shadow_names=pyenv.read_allow_shadow_names(root)
            )['to_install']
            if not to_install:
                return
            install_extras = pyenv.install_extras  # worker-safe function
        except Exception:
            return
        sys._embody_pyenv_result = None
        import threading
        import time as _time

        def worker():
            msgs = []
            try:
                res = install_extras(
                    spec, to_install,
                    lambda m, lvl='INFO': msgs.append((lvl, m)))
            except BaseException as e:
                res = {'installed': [], 'failed': {},
                       'transient': {s: str(e) for s in to_install},
                       'restart_required': [], 'deferred': ''}
            # Sys-level atomic publish: any instance's poll may consume.
            sys._embody_pyenv_result = (msgs, res)

        t = threading.Thread(target=worker, daemon=True)
        sys._embody_pyenv_installing = {'t0': _time.monotonic(), 'thread': t}
        t.start()
        run('args[0]._pollExtrasInstall()', self, delayFrames=30)

    def _pollExtrasInstall(self) -> None:
        """Main-thread poll consuming the sys-level extras result slot,
        replaying the worker's log lines, and re-arming the reconcile
        (deferred installs retry; queued requests drain)."""
        try:
            if self.my.ext.Embody is not self:
                return  # the fresh instance's init adopts the poll
        except Exception:
            return
        result = getattr(sys, '_embody_pyenv_result', None)
        if result is None:
            busy = getattr(sys, '_embody_pyenv_installing', None)
            if busy is None:
                # Nothing in flight and nothing to consume: an orphaned
                # poll (another poller already drained the slots) -- stop
                # instead of self-rescheduling for the whole session
                # (2026-08-19 review: one perpetual chain per reinit).
                return
            thread = busy.get('thread') if isinstance(busy, dict) else None
            if thread is not None and not thread.is_alive():
                # Worker died without publishing -- clear, give up loudly,
                # and keep the reconcile armed for the rest of the session.
                sys._embody_pyenv_installing = None
                self.Log('Extras install worker ended without a result -- '
                         'see the Embody log folder for details; retry '
                         'with op.Embody.ApplyDeclaredExtras().', 'ERROR')
                self._scheduleExtrasApply(delay_frames=1800)
                return
            run('args[0]._pollExtrasInstall()', self, delayFrames=30)
            return
        sys._embody_pyenv_result = None
        sys._embody_pyenv_installing = None
        msgs, res = result
        for lvl, m in msgs:
            self.Log(m, lvl)
        if res.get('deferred'):
            self.Log(f'Extras install deferred: {res["deferred"]}', 'INFO')
            self._scheduleExtrasApply(delay_frames=1800)  # ~30s at 60fps
        else:
            # Drain anything declared while this install ran.
            self._scheduleExtrasApply(delay_frames=60)

    def _checkMCPUpdate(self, installed: str):
        """Check PyPI for a newer MCP version in a background thread. Logs a
        notice if an update is available - never blocks the main thread.

        The worker touches NO TouchDesigner object: it publishes its result to
        a plain instance attribute (``self._mcp_update_notice``) and a
        main-thread poll (``_pollMCPUpdate``, scheduled via run() below) reads
        it and logs. The worker must NOT call run() itself -- run() raises
        tdError off the main thread, and the old code did exactly that inside
        the worker, so its except-clause swallowed the error and the update
        notice never logged. This mirrors EnvoyExt._beginAsyncBootstrap's
        attribute-publish + main-thread-poll marshal (self._bootstrap_result /
        _pollBootstrap).
        """
        import threading

        # Sentinel: None = worker still in flight; '' = done, no update (or the
        # network failed); a truthy (level, message) tuple = the notice to log.
        # Reset before spawning so a stale value from a prior check can't be
        # read.
        self._mcp_update_notice = None

        ceiling_major = int(self.MCP_MIN_VERSION.split('.')[0]) + 1

        def _parse(ver):
            try:
                return tuple(int(x) for x in ver.split('.'))
            except Exception:
                return None  # prerelease ('2.0.0a1') or unparseable -- skip

        def _check():
            try:
                import urllib.request
                import json
                import ssl
                # macOS's bundled Python has no default CA path (Windows
                # uses the OS store); certifi ships with TD -- load it in
                # ADDITION to defaults so HTTPS verifies on both. Never
                # disables verification.
                tls = ssl.create_default_context()
                try:
                    import certifi
                    tls.load_verify_locations(cafile=certifi.where())
                except Exception:
                    pass
                req = urllib.request.Request(
                    'https://pypi.org/pypi/mcp/json',
                    headers={'Accept': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=5,
                                            context=tls) as resp:
                    data = json.loads(resp.read())
                installed_t = _parse(installed) or ()
                # Newest stable, NON-YANKED release INSIDE the supported major
                # -- the only thing worth nagging about. Yanked releases must
                # not be recommended (a maintainer pinning MCP_MIN_VERSION to
                # one would make the range unresolvable for fresh installs).
                # Releases at/after the ceiling are a new SDK major: adopting
                # one takes a deliberate port (issue #81), so those get a calm
                # one-line note, not upgrade advice.
                in_range = []
                for rel, files in data['releases'].items():
                    v = _parse(rel)
                    if not v or v[0] >= ceiling_major:
                        continue
                    if not files or all(f.get('yanked') for f in files):
                        continue
                    in_range.append(v)
                latest_in_range = max(in_range) if in_range else None
                latest_overall = _parse(data['info']['version'])
                notice = ''
                if latest_in_range and latest_in_range > installed_t:
                    ver_str = '.'.join(str(x) for x in latest_in_range)
                    # Plain attribute write -- NOT a TD object. Read + logged on
                    # the main thread by _pollMCPUpdate.
                    notice = ('WARNING', (
                        f'MCP update available: {installed} -> {ver_str}. '
                        f'Bump EmbodyExt.MCP_MIN_VERSION in a release; every '
                        f'venv then auto-upgrades on its next start.'
                    ))
                elif latest_overall and latest_overall[0] >= ceiling_major:
                    notice = ('INFO', (
                        f'MCP {data["info"]["version"]} (new major) is out '
                        f'upstream; Embody pins <{ceiling_major} until a '
                        f'tested port lands. No action needed.'
                    ))
                self._mcp_update_notice = notice  # '' = up to date, stop polling
            except Exception:
                self._mcp_update_notice = ''  # network unavailable, not critical

        threading.Thread(target=_check, daemon=True).start()
        # Drain the worker's result on the main thread (self.Log is illegal off
        # the main thread). Re-arms while the request is in flight; bounded so a
        # worker that never publishes (e.g. daemon killed at shutdown) can't
        # re-schedule forever.
        run('args[0]._pollMCPUpdate(args[1])', self, 0, delayFrames=30)

    def _pollMCPUpdate(self, attempts: int):
        """Main-thread drain for _checkMCPUpdate's background PyPI check.

        Reads the notice the worker published on ``self._mcp_update_notice``
        and logs it via self.Log (which touches TD objects and so may only run
        here, on the main thread). Re-arms a bounded number of times while the
        worker is still in flight, then gives up silently.
        """
        # Stale-instance guard (parity with EnvoyExt._pollBootstrap): if the
        # ext reinitialized since this chain was armed, drop the stale chain --
        # the fresh instance re-arms its own on the next check.
        try:
            if self.my.ext.Embody is not self:
                return
        except Exception:
            return
        notice = getattr(self, '_mcp_update_notice', None)
        if notice is None:
            # Worker still running -- check again shortly (bounded; 40 x 30
            # frames ~= 20s at 60fps, covering a slow DNS resolve that
            # urlopen's socket timeout does not bound).
            if attempts < 40:
                run('args[0]._pollMCPUpdate(args[1])',
                    self, attempts + 1, delayFrames=30)
            return
        self._mcp_update_notice = None
        if notice:
            lvl, msg = (notice if isinstance(notice, tuple)
                        else ('WARNING', notice))
            try:
                self.Log(msg, lvl)
            except Exception:
                pass  # ext reinitialized between spawn and drain -- silent no-op

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

    def _cellVal(self, row, col, default: str = '', table=None) -> str:
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

        `table` lets a caller that already holds the DAT pass it in. The
        Externalizations property EVALUATES A PARAMETER on every access, so a
        loop reading N cells otherwise costs N par.eval() calls -- 1,626 of
        them measured in a single TDN save. Behaviour is identical either way;
        omit it and the property is read exactly as before.
        """
        if table is None:
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
            # Not every DAT is file-backed (selectDAT, mergeDAT, ...) --
            # a tracked path can resolve to one after a delete/rename
            # swap, and callers need '' ("no external path"), not an
            # AttributeError (issue #54).
            if not hasattr(oper.par, 'file'):
                return ''
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
            if not hasattr(oper.par, 'file'):
                self.Log(
                    f"Cannot set external path on {oper.path} -- "
                    f"'{oper.type}' DATs have no file parameter", "WARNING")
                return
            oper.par.file.readOnly = False
            oper.par.file = normalized
            oper.par.file.readOnly = readonly

    def _updateRowCells(self, row_key, changes: dict, strategy: str = '') -> bool:
        """Apply several cell changes to ONE externalizations row in ONE write.

        The Externalizations DAT is syncfile-backed (`externalizations.tsv`),
        so every CHANGED cell triggers a full DAT-to-file sync -- measured
        ~15ms each on a 300-row table, while an identical scratch tableDAT with
        no file costs 0.0ms. (The raw 46KB write is under 2ms of that; the rest
        is TD's own serialize + cook + dependency propagation. The A/B is what
        justifies this helper -- the exact sub-step does not.) A save that
        touched build, timestamp and dirty separately paid that cost three
        times; replaceRow coalesces them into one: 37.6ms -> 1.1ms, measured.

        Deliberately does NOT toggle par.syncfile to batch writes. That was
        tried and is unsafe: toggling it schedules a reload that lands a frame
        later and clobbers the in-memory changes with the stale file.

        Returns True when the row was written, False when nothing changed (the
        write is skipped entirely -- replaceRow touches the file even when the
        values are identical).
        """
        table = self.Externalizations
        if table is None:
            return False
        row = row_key if isinstance(row_key, int) else None
        if row is None:
            # Strategy-aware when asked: a COMP may hold BOTH a tox and a tdn
            # row, and matching on path alone would let a TDN save stamp the
            # TOX row clean (and vice versa). Callers that know their strategy
            # pass it; without one this keeps the historical first-match.
            for i in range(1, table.numRows):
                if self._cellVal(i, 'path', table=table) != row_key:
                    continue
                if strategy and self._cellVal(i, 'strategy', table=table) not in (
                        '', strategy):
                    continue
                row = i
                break
        if row is None or not (0 < row < table.numRows):
            # LOUD: the caller believes this operator is tracked. Swallowing
            # this silently let Save() write its .tox, log SUCCESS and return
            # True for a COMP whose recovery row had vanished -- and
            # dirtyHandler counts that in its "Saved N" tally, which must
            # never report a non-save.
            self.Log(f"Externalizations row not found for {row_key!r} -- "
                     f"{sorted(changes)} not recorded", "WARNING")
            return False
        headers = [self._cellVal(0, c, table=table) for c in range(table.numCols)]
        current = [self._cellVal(row, c, table=table)
                   for c in range(table.numCols)]
        updated = list(current)
        for col, val in changes.items():
            if col in headers:
                updated[headers.index(col)] = '' if val is None else str(val)
        if updated == current:
            return False
        table.replaceRow(row, updated)
        return True

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
        
        if self.my.par.Verbose:
            self.Debug(f"getOpPaths for {opToExternalize.path}:")
            self.Debug(f"  rel_directory: {rel_directory}")
            self.Debug(f"  rel_file_path: {rel_file_path}")
            self.Debug(f"  abs_folder_path: {abs_folder_path}")
            self.Debug(f"  save_file_path: {save_file_path}")
        
        return abs_folder_path, save_file_path, rel_directory, rel_file_path

    # ==========================================================================
    # ENVOY ONBOARDING
    # ==========================================================================

    def _testRunnerActive(self):
        """True iff the Embody test runner is mid-run. Decoupled from
        _smoke_test_responses (which tests replace/unstore freely), so the
        onboarding modal stays suppressed for the WHOLE run -- not just while
        seeded answers remain. Mirrors the continuity-dialog guard that already
        suppresses the OTHER modal during tests (see checkOpsForContinuity)."""
        try:
            runner = getattr(op, 'unit_tests', None)
            ext = getattr(getattr(runner, 'ext', None), 'TestRunnerExt', None)
            return bool(getattr(ext, '_running', False)) if ext else False
        except Exception:
            return False

    def _suppressDialogs(self):
        """Single source of truth for 'do not show interactive modals now'.

        True when a test run is active OR a project save is in progress
        (onProjectPreSave sets _suppress_dialogs; it is cleared after the
        post-save restore + Envoy-restart window, and again on next open via
        init()). Every _messageBox, the Verify() queue site, and _promptEnvoy
        consult this, so a save's strip/restore reinit burst can never show --
        or even queue -- the onboarding modal. Timing-independent: it is checked
        at the moment a dialog would display/queue, not via deferred scheduling."""
        if self._testRunnerActive():
            return True
        try:
            return bool(self.my.fetch('_suppress_dialogs', False, search=False))
        except Exception:
            return False

    @staticmethod
    def _wrapDialogText(message, width=70):
        """Wrap dialog prose to ~10-15 words per line (about 70 chars).

        ui.messageBox sizes itself to its longest line, so unwrapped
        prose makes screen-wide dialogs nobody can scan. Authored
        newlines are structure (paragraphs, '- ' lists) and are kept;
        each line wraps independently, with list items getting a hanging
        indent so a wrapped continuation still reads as one item. Every
        dialog in the product routes its message through this -- via
        _messageBox or at the direct ui.messageBox call sites.
        """
        wrapped = []
        for line in str(message).split('\n'):
            if not line.strip():
                wrapped.append(line)
                continue
            hang = '  ' if line.lstrip().startswith(('-', '*')) else ''
            wrapped.append(textwrap.fill(
                line, width=width, break_long_words=False,
                break_on_hyphens=False, subsequent_indent=hang))
        return '\n'.join(wrapped)

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
        if responses is not None and title in responses:
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
        # A test run is active (or seeded the store) but left THIS dialog
        # unanswered -- a genuine test gap. Surface it LOUDLY so the test
        # author seeds a response; bail with -1 rather than freezing TD on a
        # modal. This path is for tests ONLY -- never a normal save.
        if (responses is not None) or self._testRunnerActive():
            self.Log(
                f'[test] No response seeded for "{title}"; returning -1 '
                f'instead of opening modal dialog. Seed it via '
                f'op.Embody.store("_smoke_test_responses", {{...}}).',
                'WARNING')
            return -1
        # A project save is mid-flight (onProjectPreSave set _suppress_dialogs):
        # the .toe is already open for writing, so showing a modal now would
        # risk freezing the save. Return the safe default QUIETLY -- this is
        # expected, not a test, so it must not log a misleading "[test]"
        # warning on every Ctrl+S. The caller logs its own outcome (e.g. the
        # TDN at-risk skip summary names what was dropped).
        if self.my.fetch('_suppress_dialogs', False, search=False):
            self.Log(
                f'Dialog "{title}" suppressed during save -- using default '
                f'(-1).', 'DEBUG')
            return -1
        return ui.messageBox(title, self._wrapDialogText(message),
                             buttons=buttons)

    def _promptEnvoy(self):
        """Prompt user to enable Envoy (AI coding assistant integration)."""
        choice = self._messageBox('Embody - AI Coding Assistant Integration',
            'Enable Envoy?\n\n'
            'Envoy is an MCP server that lets AI coding assistants\n'
            'create, modify, and query TouchDesigner operators.\n\n'
            'Enabling it sets up the following in your project:\n'
            '  - a Python virtualenv (.venv) for the MCP bridge (~30 MB)\n'
            '  - a local MCP server on port '
            f'{self.my.par.Envoyport.eval()}\n'
            '  - AI config files: CLAUDE.md, AGENTS.md, .claude/ rules + skills\n'
            '  - .mcp.json + .embody/ (bridge, config, runtime state)\n'
            '  - .gitignore / .gitattributes entries + a .tdn git diff driver\n\n'
            'All Envoy MCP tools are auto-authorized for convenience\n'
            '(edit .claude/settings.local.json to tighten this).\n\n'
            'Fully reversible: run PreviewUninstall to see exactly what would\n'
            'be removed, then Uninstall to undo everything above.\n\n'
            'Works with Claude Code, Cursor, Windsurf, and other MCP clients.\n'
            'Change this later via the Envoyenable parameter.\n\n'
            'Note: TD will be unresponsive for a few seconds while\n'
            'dependencies install.',
            buttons=['Skip', 'Enable Envoy'])

        # choice == -1 means _messageBox suppressed the dialog (a test run OR a
        # save in progress -- _suppressDialogs) with no seeded answer. Do nothing:
        # returning here is what stops a deferred fire from flipping Envoyenable
        # off mid-save. A genuinely seeded test answer returns 0/1 (the seeded
        # path runs before suppression in _messageBox) and is still honored below.
        if choice == -1:
            return
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

    # === Setup wizard (native panel onboarding) ==============================
    # The wizard (op.Embody.op('wizard') shown via 'window_wizard') is the
    # first-run / re-openable onboarding surface. It collects the user's posture
    # and AI preferences one screen at a time, then calls _applyWizardSetup().
    # See dev/embody/plan-init-deinit-wizard.md sec 4.

    def _openSetupWizard(self):
        """Open the setup-wizard window (first run, or re-run via Setupwizard).

        Respects the same suppression as every onboarding surface: never opens
        during a test run or a project save (_suppressDialogs), so it cannot
        surprise-pop during automation. Falls back to the classic _promptEnvoy
        dialog when the wizard sub-network is absent (older / headless builds)."""
        if self._suppressDialogs():
            return
        win = self.my.op('window_wizard')
        wiz = self.my.op('wizard')
        if win is None or wiz is None:
            self.Log('Setup wizard UI not found -- using the classic Envoy '
                     'prompt.', 'DEBUG')
            self._promptEnvoy()
            return
        logic = wiz.op('logic')
        if logic is not None and hasattr(logic.module, 'start'):
            try:
                logic.module.start()   # reset to step 1 + preselect from live params
            except Exception as e:
                # Never let a render glitch swallow onboarding: still show the
                # window (in whatever state) rather than produce no dialog.
                self.Log(f'Setup wizard start() failed: {e}', 'WARNING')
        win.par.winopen.pulse()

    def _applyWizardSetup(self, mode='auto', assistant='claudecode',
                          client='', root='gitroot', custom_root='',
                          permissions='all', git='', externalize='', convoy=''):
        """Apply the setup-wizard selections and enable (or skip) Envoy.

        The single backend entry point the wizard's finish() calls. Because the
        wizard already obtained consent (the footprint step discloses everything
        Embody adds; the summary step confirms), the enable path is modal-free:
        _enableEnvoyResolved sets _consent_bulk so the config writes -- both the
        synchronous ones here AND the git/MCP writes in the deferred Start() --
        apply silently without re-prompting, in either mode. Embodymode still
        governs LATER, un-disclosed invasive actions (InitGit/InitEnvoy, a
        startup repair) via _guardFileWrite.

          mode:        'auto' | 'advanced'
          assistant:   'claudecode' | 'other' | 'none'
          client:      Aiclient menu value, used when assistant == 'other'
          root:        'gitroot' | 'projectfolder' | 'custom'
          custom_root: absolute path, used when root == 'custom'
          permissions: 'all' | 'some' | 'prompt' | 'leave' -- how Claude Code's
                       .claude/settings.local.json pre-approves Envoy MCP tools
                       (the deferred Start()'s _deploySettingsLocal reads the
                       Toolpermissions param this sets). Only meaningful for the
                       Claude Code client; ignored when Envoy is not enabled.
          git:         '' | 'gitinit' | 'gitskip' -- the wizard's git step
                       (shown only when no repo was found at wizard start).
                       'gitinit' initializes a repo at the project folder
                       BEFORE the enable path resolves the git root, so
                       config lands in the fresh repo; 'gitskip' / '' change
                       nothing (the enable path stays modal-free and simply
                       proceeds without git, as before).
          externalize: '' | 'skip' | 'auto' | 'full' -- the wizard's
                       externalize step (shown only when the project still
                       has something to externalize). 'auto' turns on the
                       Autoexternalize preference ('both') so new DATs/COMPs
                       externalize as they are created; 'full' does that AND
                       offers the project-wide externalization
                       (ExternalizeProject, which keeps its own confirmation
                       + TOX/TDN choice, and is refused outright when there
                       is no saved .toe to fall back on); 'skip' / '' change
                       nothing.
          convoy:      '' | 'enable' | 'disable' -- the wizard's independent
                       Convoy step. Sets the canonical Convoyenable toggle;
                       '' leaves it unchanged for compatibility with an older
                       wizard that had no Convoy step. Enabling ALSO installs
                       and starts the per-user host app (Register ->
                       _ensureHostApp, confirm=False): the wizard records the
                       install consent first, so the Convoy step's answer IS
                       the approval and no second dialog appears.
        """
        # Whitelist the assistant token: an unrecognized value (a typo, a
        # mis-cased 'None') must be a safe no-op, never fall through to ENABLING
        # Envoy -- the opposite of intent.
        assistant = (assistant or '').strip().lower()
        if assistant not in ('claudecode', 'other', 'none'):
            self.Log(f'Setup wizard: unrecognized assistant "{assistant}" -- no '
                     f'change made.', 'WARNING')
            return

        # Whitelist the tool-permissions token too; an unknown value falls back
        # to the safe default rather than corrupting the param.
        permissions = (permissions or 'all').strip().lower()
        if permissions not in ('all', 'some', 'prompt', 'leave'):
            permissions = 'all'

        # Whitelist the git token; anything unrecognized means "do nothing"
        # (the safe reading of a garbled choice is skip, never git init).
        git = (git or '').strip().lower()
        if git not in ('', 'gitinit', 'gitskip'):
            self.Log(f'Setup wizard: unrecognized git choice "{git}" -- '
                     f'skipping git setup.', 'WARNING')
            git = ''

        # Same reading for the externalize token: this is the one wizard
        # choice that can rewrite the WHOLE project, so anything unrecognized
        # means "do nothing", never "externalize everything".
        externalize = (externalize or '').strip().lower()
        if externalize not in ('', 'skip', 'auto', 'full'):
            self.Log(f'Setup wizard: unrecognized externalize choice '
                     f'"{externalize}" -- skipping externalization setup.',
                     'WARNING')
            externalize = ''

        # Whitelist the convoy token; anything unrecognized means "do nothing"
        # -- a garbled choice must never silently ENABLE remote LAN control.
        convoy = (convoy or '').strip().lower()
        if convoy not in ('', 'enable', 'disable'):
            self.Log(f'Setup wizard: unrecognized convoy choice "{convoy}" -- '
                     f'leaving Convoy unchanged.', 'WARNING')
            convoy = ''

        # Applying assistant='none' to any running Envoy must stop it BEFORE
        # root/client parameter writes below. Both
        # parexec handlers regenerate AI config while Envoy is on; stopping
        # first keeps the Convoy-only promise even when the wizard also moves
        # the project root during this apply pass.
        if (assistant == 'none'
                and bool(self.my.par.Envoyenable.eval())):
            self.my.par.Envoyenable = False

        # 1. Posture.
        if mode in ('auto', 'advanced'):
            self.my.par.Embodymode = mode

        # 2. Config-file location. Assigning Aiprojectroot fires parexec's
        #    _migrateRootFiles UNCONDITIONALLY (only the InitEnvoy regen is
        #    Envoyenable-gated). On first run that is benign -- nothing is at the
        #    old root yet. Set the custom PATH BEFORE flipping the mode to
        #    'custom' so the single migration resolves the real custom directory
        #    rather than the empty-path project-folder fallback.
        if root == 'custom' and custom_root:
            self.my.par.Aiprojectrootcustom = custom_root
        if root in ('gitroot', 'projectfolder', 'custom'):
            self.my.par.Aiprojectroot = root

        # 2.5 Git decision (the wizard's git step). BEFORE the assistant
        #     early-return so externalization-only users get their choice
        #     too, and BEFORE enable so _findGitRootSync / Start() see the
        #     fresh repo and land config inside it.
        if git == 'gitinit':
            self._applyWizardGitInit()

        # 2.6 Externalization decision. Also BEFORE the assistant early-return:
        #     externalization is the half of Embody that has nothing to do with
        #     the AI, so an assistant='none' user must still get their choice.
        self._applyWizardExternalize(externalize)

        # 3. AI client / assistant. Convoy does not require an attached AI
        #    client, but remote TD operations still need Envoy's loopback
        #    command server inside TouchDesigner. In that Convoy-only posture
        #    Aiclient='none' prevents launch/config generation while
        #    Envoyenable deliberately remains on as an INTERNAL substrate.
        if assistant == 'none':
            self.my.par.Aiclient = 'none'
            if convoy in ('enable', 'disable'):
                self.my.par.Convoyenable = (convoy == 'enable')
            if bool(self.my.par.Convoyenable.eval()):
                # A fresh Convoy-enable callback also starts the substrate.
                # Re-running the wizard against an already-enabled toggle has
                # no callback, so cover that path explicitly without stacking
                # a redundant Stop/Start on the fresh-enable path.
                if not bool(self.my.par.Envoyenable.eval()):
                    self._enableEnvoyResolved(configure_client=False)
                self.Log(
                    'Embody is set up without an AI coding assistant. Convoy '
                    'keeps its internal command service available, but no AI '
                    'client was configured or launched.', 'SUCCESS')
            else:
                self.my.par.Envoyenable = False
                self.Log(
                    'Embody is set up for externalization only. Turn on the AI '
                    'assistant or Convoy anytime by re-running the Setup Wizard.',
                    'SUCCESS')
            return
        if assistant == 'claudecode':
            self.my.par.Aiclient = 'claudecode'
        elif assistant == 'other' and client:
            try:
                self.my.par.Aiclient = client
            except Exception:
                self.Log(f'Unknown AI client "{client}" -- keeping the current '
                         f'selection.', 'WARNING')

        # 3.5 Convoy. Flips the canonical Convoyenable toggle -- '' means an
        #     older wizard did not show the step, so leave the setting alone.
        #     Installing/starting the per-user host app is a SEPARATE explicit
        #     pulse (ConvoyExt.InstallHost); the wizard only sets the flag.
        if convoy in ('enable', 'disable'):
            # The wizard's Convoy step IS the consent: it names the trusted
            # LAN, what enabling permits, and the background app. Record that
            # BEFORE flipping the toggle so ConvoyExt's first-enable path sees
            # it and does not raise its own long modal on top -- asking the
            # same question twice is what made this confusing.
            if convoy == 'enable':
                try:
                    comp = self.my.op('convoy')
                    if comp:
                        comp.ext.ConvoyExt.RecordInstallConsent()
                except Exception as e:
                    self.Log(f'Convoy consent not recorded: {e}', 'DEBUG')
            self.my.par.Convoyenable = (convoy == 'enable')

        # 4. Tool-permissions posture. Persist BEFORE enabling so the deferred
        #    Start()'s _deploySettingsLocal reads the chosen value. The wizard
        #    only shows this step for Claude Code; for 'other' clients it stays
        #    at the default (harmless -- settings.local.json is Claude-specific).
        self.my.par.Toolpermissions = permissions

        # 5. Enable (first run) or restart-to-apply (re-run), modal-free.
        self._enableEnvoyResolved()

    def _applyWizardGitInit(self):
        """Initialize a git repo at the project folder (wizard git step).

        Modal-free like the rest of the wizard's apply path: on failure it
        logs and setup continues without git -- exactly the state the user
        would have been in anyway, recoverable via op.Embody.InitGit().

        Runs under _consent_bulk: the wizard's git step IS the consent
        (the user explicitly chose 'Initialize Git' after reading what it
        does), so the .gitignore/.gitattributes writes inside
        init_git_repo must not re-prompt in Advanced mode -- this step
        runs BEFORE _enableEnvoyResolved sets the batch-wide consent."""
        from pathlib import Path
        self._consent_bulk = True
        try:
            initialized = mod.envoy_setup.init_git_repo(
                self.my.ext.Envoy, Path(project.folder).resolve())
            if initialized is None:
                self.Log('Git init failed -- continuing without git. Run '
                         'op.Embody.InitGit() to retry later.', 'WARNING')
        except Exception as e:
            self.Log(f'Git init failed: {e} -- continuing without git.',
                     'WARNING')
        finally:
            self._consent_bulk = False

    def _applyWizardExternalize(self, externalize=''):
        """Apply the wizard's externalization choice (step 2.6).

        ''/'skip' = nothing; 'auto' = auto-externalize new ops; 'full' =
        that plus the project-wide sweep. 'full' is the one wizard action
        touching the whole project (a project-wide re-tag destroyed 18
        specimen .tdn on 2026-07-01, see destructive-tests.md): it only
        calls ExternalizeProject() -- which keeps its own confirmation --
        and refuses without a reopenable .toe recovery point on disk
        (RunDestructiveTests' invariant). Never raises."""
        token = (externalize or '').strip().lower()
        if token not in ('auto', 'full'):
            return

        # Both choices mean "keep new work externalized from here on".
        try:
            self.my.par.Autoexternalize = 'both'
            self.Log('Auto-externalization ON -- new COMPs and DATs are written '
                     'to disk as they are created (change it anytime via the '
                     'Auto-Externalize New Ops parameter).', 'SUCCESS')
        except Exception as e:
            self.Log(f'Could not turn on auto-externalization: {e}', 'WARNING')

        if token != 'full':
            return

        recovery = self._wizardRecoveryPoint()
        if not recovery:
            self.Log('Skipping the whole-project externalization: no saved .toe '
                     'on disk to fall back on, and it re-tags every COMP and DAT '
                     'in the project. Save the project first, then run '
                     'Externalize Full Project from the Embody parameters. New '
                     'work is still externalized automatically.', 'WARNING')
            return
        self.Log(f'Whole-project externalization requested -- it re-tags every '
                 f'compatible COMP and DAT. Recovery point on disk: '
                 f'"{recovery}". Confirm the format in the dialog.', 'WARNING')
        self._scheduleProjectExternalization()

    # TouchDesigner's never-saved placeholder name. A project TD has never
    # written to disk is 'NewProject.toe' (and 'NewProject.N.toe' once an
    # incremental save has bumped it). ANY other name means the user saved
    # this project somewhere -- that is the whole test. See
    # _projectSavedOnDisk for why the on-disk file is NOT a reliable test.
    _PLACEHOLDER_TOE_RE = re.compile(r'^NewProject(\.\d+)?\.toe$',
                                     re.IGNORECASE)

    @staticmethod
    def _projectNameIsPlaceholder() -> bool:
        """True while this project still carries TD's never-saved name.

        An unreadable/empty project.name reads as a placeholder, so the
        conservative answer (treat as unsaved) survives. Never raises."""
        try:
            name = str(project.name or '').strip()
        except Exception:
            return True
        if not name:
            return True
        return bool(EmbodyExt._PLACEHOLDER_TOE_RE.match(name))

    @staticmethod
    def _resolveProjectToe():
        """The .toe file on disk this project came from, or None.

        project.folder / project.name is only the FIRST candidate, never the
        verdict: after an incremental save TouchDesigner reports project.name
        as the NEXT name in the series while disk still holds the current one
        ('D:/node-touchdesigner/Control.35.toe' saved, project.name
        'Control.36.toe'). Checking only the literal path therefore calls a
        long-saved production project unsaved (field-reported 2026-08-19,
        and the same drift blocked the smoke harness on 2026-08-10). So fall
        back to the newest file in the same increment series. Never raises."""
        try:
            folder = str(project.folder or '')
            name = str(project.name or '')
            if not folder or not name:
                return None
            literal = os.path.join(folder, name)
            if os.path.isfile(literal):
                return literal
            # Same series, any increment: 'Control.toe', 'Control.35.toe'.
            stem = name[:-4] if name.lower().endswith('.toe') else name
            base = re.sub(r'\.\d+$', '', stem)
            if not base:
                return None
            pattern = re.compile(r'^' + re.escape(base) + r'(\.\d+)?\.toe$',
                                 re.IGNORECASE)
            newest, newest_mtime = None, -1.0
            for entry in os.listdir(folder):
                if not pattern.match(entry):
                    continue
                candidate = os.path.join(folder, entry)
                try:
                    if not os.path.isfile(candidate):
                        continue
                    mtime = os.path.getmtime(candidate)
                except Exception:
                    continue
                if mtime > newest_mtime:
                    newest, newest_mtime = candidate, mtime
            return newest
        except Exception:
            return None

    def _wizardRecoveryPoint(self):
        """The saved .toe a whole-project externalization can be undone by
        reopening, or None when there is none on disk.

        Resolves the real file (see _resolveProjectToe) rather than trusting
        project.name, and never project.modified / project.dirty -- those two
        proxies have failed here, in opposite directions; see
        .claude/rules/destructive-tests.md rule 3. This one deliberately
        stays file-based: an unreachable .toe is no recovery point, whatever
        the project is named. Never raises."""
        return self._resolveProjectToe()

    def _projectSavedOnDisk(self):
        """True once this project has been saved somewhere.

        THE gate for every self-initiated disk write (logs, tsv, .embody
        state) -- unsaved writes land in TD's default folder and are
        orphaned by the wizard's save. Single authority; ConvoyExt._savedToe
        and the wizard delegate here. Latched (a project cannot become
        unsaved within a session).

        The NAME is the verdict: anything but TD's 'NewProject[.N].toe'
        placeholder = saved. Never require a file at project.folder /
        project.name -- TD reports the NEXT incremental name after a save,
        so that path is routinely absent on a saved project (refused
        Enable Convoy on a production box, 2026-08-19). Never raises."""
        if getattr(self, '_saved_on_disk', False):
            return True
        if not self._projectNameIsPlaceholder():
            self._saved_on_disk = True
            return True
        if self._resolveProjectToe() is not None:
            self._saved_on_disk = True
            return True
        return False

    def _scheduleProjectExternalization(self):
        """Run ExternalizeProject() a few frames out (wizard externalize step).

        Deferred so its modal opens after the wizard's apply path has fully
        unwound (window closed, params written, Envoy enable kicked off) --
        a modal raised mid-apply would stall the rest of setup behind it.
        Isolated in its own method so tests can stub the schedule."""
        try:
            run(f"op('{self.my}').ext.Embody.ExternalizeProject()",
                delayFrames=30, fromOP=self.my)
        except Exception as e:
            self.Log(f'Could not start the whole-project externalization: {e} '
                     f'-- run Externalize Full Project manually.', 'WARNING')

    # Root COMPs TouchDesigner owns, not user content -- never counted when
    # judging whether a project externalizes itself. ('local' / 'perform' are
    # skipped the same way by _cleanupEmptyDirectories.)
    _BUILTIN_ROOT_COMPS = frozenset({'local', 'perform', 'sys', 'ui'})

    def _projectLooksExternalized(self) -> bool:
        """Best-effort: does this project already write itself out to disk?

        The wizard's externalize step is an OFFER, not a repair, so it is
        hidden when every top-level COMP the user owns already externalizes --
        itself tagged, or holding tracked content somewhere inside. Cheap by
        design: the tracked paths come from the externalizations table (one
        pass), and only root's direct children are examined -- no op-tree
        recursion.

        Conservative in exactly one direction: an empty table, a top-level COMP
        with nothing tracked under it, a project with no user COMPs at all, or
        ANY exception returns False, i.e. SHOW the step. A needlessly shown step
        costs one click; a wrongly hidden one silently denies the feature.
        Never raises.
        """
        try:
            table = self.Externalizations
            if table is None or table.numRows < 2:
                return False        # nothing tracked at all -- always offer
            embody_root = self.my.path
            embody_prefix = embody_root + '/'
            tracked = set()
            for i in range(1, table.numRows):
                path = self._cellVal(i, 'path')
                # Embody's own subtree does not count as the project
                # externalizing ITSELF -- otherwise merely installing Embody
                # would make its container look already-externalized.
                if path and not (path == embody_root
                                 or path.startswith(embody_prefix)):
                    tracked.add(path)
            if not tracked:
                return False
            tox_tag = self.my.par.Toxtag.val
            tdn_tag = self.my.par.Tdntag.val
            candidates = 0
            for child in self.root.children:
                if child.family != 'COMP':
                    continue
                if child.name in self._BUILTIN_ROOT_COMPS:
                    continue
                if child.path == embody_root:
                    continue
                if not self.isOpProcessable(child) or self.isReplicant(child):
                    continue
                candidates += 1
                if tox_tag in child.tags or tdn_tag in child.tags:
                    continue
                prefix = child.path + '/'
                if any(p == child.path or p.startswith(prefix) for p in tracked):
                    continue
                return False        # untracked user content -- offer
            return candidates > 0
        except Exception:
            return False

    def _enableEnvoyResolved(self, configure_client=True):
        """Modal-free Envoy enable/refresh, used by the setup wizard.

        Skips the interactive git modal that _enableEnvoy() pops: the wizard
        already chose the config location (Aiprojectroot) and disclosed the git
        changes, so consent exists. Resolves the git root silently (via
        _findGitRootSync -- Start() re-resolves the same way). If the project is
        not in a git repo, client config is still generated when requested,
        with no .gitignore / .gitattributes edits.

        ``configure_client=False`` is Convoy-only mode: start the same internal
        loopback command server, but do not extract or configure an AI coding
        client. Aiclient must already be ``none``; EnvoyExt also enforces that
        distinction on every later watchdog restart.

        Two paths:
        - FIRST RUN (Envoy off): optionally write AI config, then flip
          Envoyenable so
          parexec launches Start(), whose async bootstrap builds the venv OFF
          the main thread (do NOT call _setupEnvironment() here -- that is the
          blocking path _enableEnvoy uses).
        - RE-RUN (Envoy already on, via the Setup Wizard button): the param
          changes above already regenerated config through parexec; a
          `Envoyenable = True` would be a no-op and never restart the server, so
          the running server would keep its OLD port/root/client. RESTART it
          explicitly so the new selections take effect, and only then set the
          'Starting...' status (setting it on a no-op would lie forever)."""
        already_on = bool(self.my.par.Envoyenable.eval())

        # Resolve + record the git root without a prompt so _extractAIConfig's
        # _findProjectRoot('gitroot') and Start() agree on one location.
        git_root = self._findGitRootSync()  # Path or 'no-git'
        self.my.store('_git_root', str(git_root))
        if git_root == 'no-git':
            if configure_client:
                self.Log('No git repo found -- generating MCP + AI config only '
                         '(no .gitignore / .gitattributes). Run '
                         'op.Embody.InitGit() later to add git integration.',
                         'INFO')
            else:
                self.Log('No git repo found -- starting only Convoy\'s internal '
                         'command service; no AI-client or git config will be '
                         'generated.', 'INFO')

        if already_on:
            # Re-run: config already regenerated by parexec on the param changes.
            # Restart so the runtime binds the new port/root/client.
            self.Log('Re-applying Envoy setup...', 'INFO')
            self.my.ext.Envoy.Stop()
            self.my.par.Envoystatus = 'Starting...'
            run(f"op('{self.my}').ext.Envoy.Start()", delayFrames=10)
            if configure_client:
                self.Log(f'Envoy setup updated for {self._aiClientLabel()}.',
                         'SUCCESS')
            else:
                self.Log('Convoy internal command service restarted.', 'SUCCESS')
            return

        # First enable. When a client is selected, the wizard's footprint step
        # disclosed + the user confirmed the config writes, so consent them as
        # one batch. Convoy-only mode performs none of those writes and therefore
        # does not raise the bulk-consent flag.
        self.Log('Setting up Envoy...' if configure_client else
                 'Starting Convoy internal command service...', 'INFO')
        if configure_client:
            self._consent_bulk = True
            run(f"op('{self.my}').ext.Embody._consent_bulk = False",
                delayFrames=7200)
            # AI config now (fast, no venv needed). The heavy venv build + pip
            # install runs asynchronously inside Start() (_beginAsyncBootstrap).
            self._extractAIConfig()
        # Flip Envoyenable -> parexec kicks Envoy.Start() (async bootstrap).
        # Client-selected git/MCP writes run under the still-set bulk consent;
        # Convoy-only Start() skips them.
        self.my.par.Envoyenable = True
        self.my.par.Envoystatus = 'Starting...'
        if configure_client:
            self.Log(
                f'Envoy enabled! Config generated for {self._aiClientLabel()}. '
                f'Dependencies install in the background; MCP connects when ready.',
                'SUCCESS'
            )
        else:
            self.Log(
                'Convoy command service enabled. Dependencies install in the '
                'background; no AI client configuration was generated.',
                'SUCCESS')

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
            # local.json is the regenerable machine-local pin (A-14):
            # delete rather than move, the next startup rewrites it at
            # the new root -- leaving it would strand the old .embody/
            # dir and orphan a stale pin.
            for name in ('envoy.json', 'envoy-bridge.py',
                         'envoy-tools-cache.json',
                         '.envoy-tools-cache.json', 'local.json'):
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

    # === Mode + consent (Auto vs Advanced) ===================================
    # Embodymode governs Embody's posture toward invasive, project-level actions:
    #   auto (default) -- manage everything, act silently (today's behavior)
    #   advanced       -- ask before each such action (a batched confirm)
    # _guard is the single chokepoint every gated action flows through, so the
    # two postures stay ONE code path (cheap to maintain + test). It reads the
    # mode via getattr, so it degrades to 'auto' until the Embodymode param is
    # authored on the COMP (which needs a save).
    # See dev/embody/plan-init-deinit-wizard.md sec 1c.

    def _embodyMode(self):
        """Current posture: 'auto' (default) or 'advanced'. Falls back to 'auto'
        until the Embodymode param is authored, so behavior is unchanged today."""
        par = getattr(self.my.par, 'Embodymode', None)
        return par.eval() if par is not None else 'auto'

    def _guard(self, title, message, apply_fn, mode=None):
        """Gate ONE invasive action by Embody mode. Returns True if applied.

          auto     -> apply immediately (silent).
          advanced -> confirm via _messageBox (Apply/Skip); apply only on Apply.

        A suppressed / headless dialog (_messageBox -> -1) DECLINES in advanced:
        when the user asked to be consulted, never act without an explicit yes.
        In auto, headless still applies (safe managed default). This respects the
        existing test/save dialog suppression -- it never opens a modal in a
        context where _messageBox is gated."""
        mode = mode if mode is not None else self._embodyMode()
        if mode != 'advanced':
            apply_fn()
            return True
        if self._messageBox(title, message, ['Apply', 'Skip']) == 0:
            apply_fn()
            return True
        return False

    # _consent_bulk: set by an orchestrator (InitGit / InitEnvoy) or the setup
    # wizard once it has shown ONE combined confirm for a whole category, so the
    # individual _guardFileWrite sub-calls it makes apply silently instead of
    # each popping its own dialog. This keeps Advanced mode's per-category prompt
    # from fragmenting into one-dialog-per-file. Set/cleared in a try/finally (or,
    # for the wizard's async span, cleared in _continueStart + a bounded timer)
    # so it can never stay stuck (which would silence all guards).
    _consent_bulk = False
    # _startup_config_pass: set by _continueStart / _upgradeEnvoy around the
    # auto-config writes that run on project OPEN. In Advanced mode a modal must
    # NOT pop there (it would block the frame-30..80 restore chain), so guards
    # DEFER with a breadcrumb instead of prompting.
    _startup_config_pass = False

    def _guardFileWrite(self, category, action, details, apply_fn, mode=None):
        """Gate an invasive write to the USER'S repo, showing EXACTLY which
        files/entries change so Advanced mode never surprises them.

        Call ONLY when a real change is pending (after the idempotency check),
        so a no-op never prompts. Behavior:
          consented batch (_consent_bulk) -> apply_fn() silently.
          auto                            -> apply_fn() silently.
          advanced + startup Start        -> DEFER + breadcrumb (never a modal on
                                             open; never a silent write).
          advanced + interactive          -> Apply/Skip listing `details`.
          advanced + save/test            -> DECLINE via _guard (_messageBox -1).

        category: short noun for the dialog title (e.g. 'Git config').
        action:   verb phrase naming the effect + location ('update .gitignore
                  at /repo').
        details:  list of strings shown as bullets -- the exact file paths, or
                  the exact lines/entries being written.
        Returns True if applied."""
        if self._consent_bulk:
            apply_fn()
            return True
        mode = mode if mode is not None else self._embodyMode()
        if mode != 'advanced':
            apply_fn()
            return True
        if self._startup_config_pass:
            # Startup Start on open: never block, never write silently -- defer
            # with an actionable breadcrumb (only reached when a real change is
            # pending, so clean opens stay quiet).
            self.Log(f'Advanced mode: deferred a repo change on open ({action}). '
                     f'Nothing was written -- run op.Embody.InitEnvoy() / '
                     f'InitGit(), or set Embodymode to Auto, to apply it.', 'INFO')
            return False
        bullets = '\n'.join(f'  - {d}' for d in details) if details else ''
        msg = (f'Embody will {action}:\n\n{bullets}\n\nApply this change?'
               if bullets else
               f'Embody will {action}.\n\nApply this change?')
        return self._guard(f'Embody - {category}', msg, apply_fn, mode='advanced')

    # === Uninstall / Deinit ==================================================
    # Reversible teardown of Embody's project footprint. The planner/executor
    # (compute_uninstall_plan .. uninstall) + the _UNINSTALL_MARKER_* constants
    # live in embody_admin (WP7c); only _uninstallClassifyMarker (marker + hash
    # classification, shared with the AI-config hash manifest) stays on the
    # facade. See dev/embody/plan-init-deinit-wizard.md sec 5.

    def _uninstallClassifyMarker(self, path, root, hashes):
        """Classify a candidate file: 'delete' (Embody-generated + unmodified),
        'review' (marker present but edited -> keep + flag), or None (no marker
        -> not ours, ignore)."""
        try:
            content = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            return None
        if self._EMBODY_MARKER not in content:
            return None
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except Exception:
            rel = path.name
        stored = hashes.get(rel)
        if stored is None or self._contentHash(content) == stored:
            return 'delete'
        return 'review'  # user edited a generated file -> preserve it

    def _computeUninstallPlan(self, target_dir=None):
        """NON-DESTRUCTIVE uninstall plan (delete/strip/unset/review/missing) -- see embody_admin."""
        return mod.embody_admin.compute_uninstall_plan(self, target_dir)

    def PreviewUninstall(self, target_dir=None):
        """Log + return a NON-DESTRUCTIVE preview of a full Uninstall. Nothing is
        removed. Use this to review the reversal plan before running Uninstall.
        See embody_admin."""
        return mod.embody_admin.preview_uninstall(self, target_dir)

    def _removeTreeWithin(self, path, root):
        """Recursively remove a directory only if it resolves inside root -- see embody_admin."""
        return mod.embody_admin.remove_tree_within(self, path, root)

    def _stripMarkedBlock(self, text, marker):
        """Return text with Embody's marked comment block removed -- see embody_admin."""
        return mod.embody_admin.strip_marked_block(self, text, marker)

    def _stripMcpEnvoy(self, path):
        """Remove only the 'envoy' server from a .mcp.json -- see embody_admin."""
        return mod.embody_admin.strip_mcp_envoy(self, path)

    def _executeUninstallPlan(self, plan, include_review=False):
        """Execute a plan from _computeUninstallPlan (destructive) -- see embody_admin."""
        return mod.embody_admin.execute_uninstall_plan(self, plan, include_review=include_review)

    def Uninstall(self, confirm=False, include_review=False, target_dir=None):
        """Reverse Embody's project footprint. DESTRUCTIVE -- requires
        confirm=True (review PreviewUninstall() first). 'review' items are KEPT
        unless include_review=True; user files are never deleted. See embody_admin."""
        return mod.embody_admin.uninstall(
            self, confirm=confirm, include_review=include_review,
            target_dir=target_dir)

    def UninstallHandler(self, target_dir=None):
        """Uninstall pulse handler: preview the footprint, confirm via
        ui.messageBox, then run Uninstall on Yes. See embody_admin."""
        return mod.embody_admin.uninstall_handler(self, target_dir=target_dir)

    # AI-client tokens -> the config files _extractAIConfig writes for them (on
    # top of AGENTS.md, which is always written). Used to list the exact files
    # in the Advanced-mode confirm.
    _AI_CONFIG_FILES = {
        'claudecode': ['CLAUDE.md (or ENVOY.md)', '.claude/rules/', '.claude/skills/'],
        'opencode':   ['opencode.json', '.claude/rules/', '.claude/skills/'],
        'cursor':     ['.cursor/rules/'],
        'copilot':    ['.github/copilot-instructions.md'],
        'windsurf':   ['.windsurf/rules/'],
        'gemini':     ['GEMINI.md'],
    }

    def _extractAIConfig(self):
        """Extract AI coding assistant config files based on par.Aiclient -- see embody_git."""
        return mod.embody_git.extract_ai_config(self)

    def _writeAgentsMd(self, target_dir):
        """Write AGENTS.md -- universal AI instructions for all major AI tools -- see embody_git."""
        return mod.embody_git.write_agents_md(self, target_dir)

    def _writeClaudeCodeConfig(self, target_dir):
        """Write Claude Code config: CLAUDE.md + .claude/rules/ + .claude/skills/ -- see embody_git."""
        return mod.embody_git.write_claude_code_config(self, target_dir)

    def _stripFrontmatter(self, content):
        """Strip leading YAML frontmatter (---...---) from content -- see embody_git."""
        return mod.embody_git.strip_frontmatter(self, content)

    def _writeCursorRules(self, target_dir):
        """Write Cursor rules: .cursor/rules/{slug}.mdc with frontmatter -- see embody_git."""
        return mod.embody_git.write_cursor_rules(self, target_dir)

    def _writeCopilotInstructions(self, target_dir):
        """Write GitHub Copilot config: combined + per-rule files -- see embody_git."""
        return mod.embody_git.write_copilot_instructions(self, target_dir)

    def _writeWindsurfRules(self, target_dir):
        """Write Windsurf rules: .windsurf/rules/{slug}.md -- see embody_git."""
        return mod.embody_git.write_windsurf_rules(self, target_dir)

    def _writeGeminiConfig(self, target_dir):
        """Write Gemini CLI config: GEMINI.md importing AGENTS.md -- see embody_git."""
        return mod.embody_git.write_gemini_config(self, target_dir)

    def _writeClaudeMd(self, target_dir):
        """Write CLAUDE.md from the text_claude template DAT -- see embody_git."""
        return mod.embody_git.write_claude_md(self, target_dir)

    # Sidecar manifest of {rel_path: sha256(generated content)} recorded under the
    # project root. It lets _writeTemplate tell "untouched since we wrote it"
    # (safe to regenerate) from "the user edited a generated file" (preserve),
    # WITHOUT mutating the generated files themselves (they stay byte-identical to
    # the templates). Committing it makes the protection survive a fresh clone.
    _HASH_MANIFEST = '.embody/generated-hashes.json'

    def _contentHash(self, content):
        """Stable 16-hex SHA-256 of a generated file's content -- see embody_git."""
        return mod.embody_git.content_hash(self, content)

    def _loadHashManifest(self, target_dir):
        """Load the sidecar generated-file hash manifest -- see embody_git."""
        return mod.embody_git.load_hash_manifest(self, target_dir)

    def _saveHashManifest(self, target_dir, manifest):
        """Save the sidecar generated-file hash manifest -- see embody_git."""
        return mod.embody_git.save_hash_manifest(self, target_dir, manifest)

    # --- Install manifest -----------------------------------------------------
    # Records Embody's project footprint so Uninstall/Deinit can reverse it
    # PRECISELY and SAFELY -- above all, never delete a file that predated
    # Embody. Additive + best-effort: a manifest write must never break the
    # footprint action it records. See dev/embody/plan-init-deinit-wizard.md #6.
    _INSTALL_MANIFEST = '.embody/manifest.json'

    def _installManifestSkeleton(self):
        """Empty install-manifest structure -- see embody_git."""
        return mod.embody_git.install_manifest_skeleton(self)

    def _loadInstallManifest(self, target_dir):
        """Load the install manifest (footprint record) -- see embody_git."""
        return mod.embody_git.load_install_manifest(self, target_dir)

    def _saveInstallManifest(self, target_dir, manifest):
        """Save the install manifest -- see embody_git."""
        return mod.embody_git.save_install_manifest(self, target_dir, manifest)

    def _manifestRelPath(self, target_dir, path):
        """Stored (relative/absolute POSIX) form for a footprint path -- see embody_git."""
        return mod.embody_git.manifest_rel_path(self, target_dir, path)

    def _manifestRecordCreatedFile(self, target_dir, path):
        """Record a file Embody CREATED (best-effort) -- see embody_git."""
        return mod.embody_git.manifest_record_created_file(self, target_dir, path)

    def _manifestRecordAppendedFile(self, target_dir, path, marker, kind='block'):
        """Record a SHARED file Embody modified in place -- see embody_git."""
        return mod.embody_git.manifest_record_appended_file(self, target_dir, path, marker, kind)

    def _manifestRecordVenv(self, target_dir, venv_path):
        """Record that Embody CREATED the venv -- see embody_git."""
        return mod.embody_git.manifest_record_venv(self, target_dir, venv_path)

    def _manifestUnrecordCreatedFile(self, target_dir, path):
        """Forget a files_created record (Embody removed the file itself) -- see embody_git."""
        return mod.embody_git.manifest_unrecord_created_file(self, target_dir, path)

    def _manifestRecordGitConfig(self, target_dir, keys):
        """Record git config keys Embody set -- see embody_git."""
        return mod.embody_git.manifest_record_git_config(self, target_dir, keys)

    def _writeTemplate(self, target_dir, rel_path, content):
        """Write one template file, respecting the Embody/Envoy marker -- see embody_git."""
        return mod.embody_git.write_template(self, target_dir, rel_path, content)

    # ==========================================================================
    # STATUS READOUT (the rows the node viewer draws)
    # ==========================================================================
    #
    # THE READOUT DERIVES FROM PARAMETERS, so nothing here feeds it values.
    # There used to be a second path: seven wrappers that published each
    # startup phase's counts into Embody/startup_progress for a column of
    # progress bars to draw. The bars are gone, the record they drew from is
    # gone, and the wrappers went with them rather than being kept warm --
    # a write-only publish is indistinguishable from a working one until
    # somebody trusts it, and by the time the bars died one of those calls
    # was already invoking a function that no longer existed, swallowed by
    # its own `except Exception: pass`.
    #
    # What survives is the redraw: the panel is event-driven and a value the
    # readout shows has to say so, or the row it just wrote is never drawn.

    def _republishStatusPanel(self) -> None:
        """Redraw the status readout, because THIS was an event.

        The panel is event-driven: it holds no clock of its own and cooks
        nothing until something tells it a value moved (see
        viz_status/status_publish). A parameter the readout shows is normally
        caught by its parameter-execute DAT -- but a write that lands before
        that DAT is watching (the frame-0 auto-save seed) has nothing behind
        it, so it has to say so here. Best-effort: a viewer that cannot
        redraw must never break the startup it is reporting on."""
        try:
            publisher = self.my.op('viz_status/status_publish')
            if publisher is not None:
                publisher.module.Refresh()
        except Exception:
            pass

    def _upgradeEnvoy(self):
        """Restore AI config on open if Envoy is enabled but files are missing -- see embody_git."""
        return mod.embody_git.upgrade_envoy(self)

    def _clientFilesMissing(self, target_dir, client):
        """True if the primary config files for the selected client are absent -- see embody_git."""
        return mod.embody_git.client_files_missing(self, target_dir, client)

    def _aiClientLabel(self):
        """Human label of the SELECTED Aiclient option (e.g. 'Claude Code') -- see embody_git."""
        return mod.embody_git.ai_client_label(self)

    def InitEnvoy(self) -> None:
        """(Re)generate all Envoy + AI client config files (idempotent). Requires
        Envoy enabled (par.Envoyenable = True). See embody_git."""
        return mod.embody_git.init_envoy(self)

    def InitGit(self) -> None:
        """Initialize/reconnect a git repo, then regenerate git + MCP + AI config so
        paths point to the git root. Requires Envoy enabled. See embody_git."""
        return mod.embody_git.init_git(self)

    # ==========================================================================
    # INITIALIZATION & RESET
    # ==========================================================================

    def Reset(self, removeTags: bool = False) -> None:
        """Reset Embody to initial state -- see embody_git."""
        return mod.embody_git.reset(self, removeTags)

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

        # Migration 0: blank legacy 'dirty' cells ONCE (2026-08-20).
        # Dirty is runtime-only now; the column stays for schema compat
        # but persisted values are legacy churn. One normalization write,
        # then the tsv never changes from dirty flips again.
        if 'dirty' in headers:
            blanked = 0
            for i in range(1, table.numRows):
                if str(self._cellVal(i, 'dirty') or ''):
                    table[i, 'dirty'] = ''
                    blanked += 1
            if blanked:
                migrations.append(
                    f'blanked {blanked} legacy dirty cell(s) '
                    f'(dirty state is runtime-only now)')

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
            self.Log(f'Schema migration: {", ".join(migrations)}', 'SUCCESS')

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
        """Path to .embody/config.json -- see embody_admin."""
        return mod.embody_admin.settings_path(self)

    def _findSettingsFile(self) -> Optional[Path]:
        """Locate .embody/config.json across candidate roots -- see embody_admin."""
        return mod.embody_admin.find_settings_file(self)

    def _projectJsonPath(self) -> Path:
        """Path to .embody/project.json (committed project metadata) -- see embody_admin."""
        return mod.embody_admin.project_json_path(self)

    def _writeProjectJson(self) -> None:
        """Steward .embody/project.json and pin td_build machine-locally.

        A-14: the pin lives in .embody/local.json (machine-local); the
        tracked project.json is stewarded with key-level ownership (the
        retired td_build key removed once, everything else preserved,
        unreadable JSON never overwritten) -- see embody_admin.
        """
        # A never-saved project has no real .embody home yet (the frame-80
        # onStart schedule reaches here on unsaved untitled projects
        # spawned from a startup .toe). onProjectPostSave calls this
        # again, so the files appear with the first save.
        if not self._projectSavedOnDisk():
            return
        try:
            mod.embody_admin.write_local_json(self)
        except Exception as e:
            # The machine-local pin must never block the tracked-file
            # steward (panel finding: an unexpected raise here silently
            # stopped A-14 stewardship on that machine).
            self.Log(f'local.json pin failed: {e}', 'WARNING')
        return mod.embody_admin.write_project_json(self)

    # --- project.json 'convoy' key (Convoy Phase 2) -----------------------
    # ConvoyExt lives on the 'convoy' child COMP, where `mod.embody_admin`
    # does not resolve (mod searches from the calling DAT's own network).
    # These are its only door to the steward, and they keep the delegating
    # -stub contract this file already follows for every embody_admin call.

    def _readConvoyEntry(self) -> dict:
        """The 'convoy' object from .embody/project.json, or {} -- see
        embody_admin. Never raises."""
        return mod.embody_admin.read_convoy_entry(self)

    def _readConvoyId(self) -> str:
        """This project's convoy id, or '' -- see embody_admin."""
        return mod.embody_admin.read_convoy_id(self)

    def _readConvoyBindingState(self) -> str:
        """The safe candidate/established project binding interpretation."""
        return mod.embody_admin.read_convoy_binding_state(self)

    def _mintConvoyId(self) -> str:
        """A candidate convoy id ('cv_' + 16 hex), unwritten -- so the
        first-enable confirmation can NAME the id before minting it."""
        return mod.embody_admin.mint_convoy_id()

    def _ensureConvoyId(self, convoy_id=None, consent_scope=None,
                        binding_state=None) -> str:
        """Record the convoy key (id + consent scope + grant time) with
        key-level ownership; return the id in force, or '' -- see
        embody_admin. Explicit-enable path only."""
        kwargs = {}
        if consent_scope is not None:
            kwargs['consent_scope'] = consent_scope
        if binding_state is not None:
            kwargs['binding_state'] = binding_state
        return mod.embody_admin.ensure_convoy_id(self, convoy_id, **kwargs)

    def _adoptConvoyId(self, convoy_id, expected_id,
                       binding_state='established') -> str:
        """Persist a host-authoritative automatic realm using a CAS guard."""
        return mod.embody_admin.adopt_convoy_id(
            self, convoy_id, expected_id, binding_state)

    def _rebindConvoyToCandidate(self, expected_id) -> str:
        """User-confirmed rejoin: demote the realm binding to candidate."""
        return mod.embody_admin.rebind_convoy_to_candidate(
            self, expected_id)

    def _saveSettings(self) -> None:
        """Persist whitelisted parameter values to .embody/config.json -- see embody_admin."""
        return mod.embody_admin.save_settings(self)

    def _deferSaveSettings(self) -> None:
        """Schedule a settings save on the next frame (coalesces) -- see embody_admin."""
        return mod.embody_admin.defer_save_settings(self)

    def _restoreSettings(self, kick_envoy: bool = False) -> bool:
        """Restore parameter values from .embody/config.json (returns True if
        restored; sets _restoring_settings during the write). See embody_admin."""
        return mod.embody_admin.restore_settings(self, kick_envoy=kick_envoy)

    def _showTDNMigrationNudge(self) -> None:
        """One-time dialog after upgrading from the binary Tdnenable toggle -- see embody_admin."""
        return mod.embody_admin.show_tdn_migration_nudge(self)

    def Verify(self) -> None:
        """Initialize or reconnect Embody on install or update.

        Called from execute.py onCreate() after CreateExternalizationsTable()
        has already run.  Two scenarios:

        - Fresh install: table exists but is empty (just created) -- skip dialog,
          run UpdateHandler quietly, then offer Envoy opt-in.
        - Update install: table has prior data -- validate tracked operators
          quietly (no dialog; see _validateTrackedOperators).
        """
        # Restore saved settings from a previous install before any dialogs.
        settings_restored = self._restoreSettings()

        # Toxtag is the fingerprint custom par: present on every Embody build
        # (the old marker, Addtagshort, was removed with the editable-shortcuts
        # redesign in 6.0.117 -- issue #50).
        embodies = op('/').findChildren(name='Embody', parName='Toxtag')
        other_embody = next((e for e in embodies if e != self.my), None)

        if other_embody:
            self._messageBox('Embody',
                f'An instance of Embody already exists:\n{other_embody}\n'
                'Please remove it first.', buttons=['Ok'])
            return

        table = self.Externalizations
        has_prior_data = table and table.numRows > 1

        if has_prior_data:
            # UPDATE scenario (surviving table): validate quietly via the
            # deferred UpdateHandler() -- the old Re-scan dialog wired to
            # Reset(), which unlinked EVERY tracked file then re-exported
            # the project in one frame (minutes-long freeze, zero files on
            # disk in the crash window). Ground-up rebuild stays available
            # via Disable -> Enable, which discloses the deletion.
            self._validateTrackedOperators()
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
        elif settings_restored:
            # Returning user in this project ROOT, but no table data yet --
            # e.g. an untitled project spawned from a default startup file,
            # or a fresh drop into a folder Embody was configured in before.
            # The persisted config.json already records the user's Envoy
            # decision; honor it instead of re-asking on every new project
            # (issue #60: the opt-in modal re-queued per project forever).
            if self.my.par.Envoyenable.eval():
                run(f"op('{self.my}').ext.Envoy.Start()", delayFrames=60)
        else:
            # Genuinely fresh install (empty table, no config.json found
            # anywhere): prompt for opt-in. A found config.json takes the
            # elif above and honors its persisted decision -- no re-prompt
            # nagging on untitled projects (issue #60). Never queue while
            # dialogs are suppressed (one of three display-time gates with
            # _promptEnvoy/_messageBox; queuing here also reset
            # Envoyenable=False). Idempotent.
            if (not self._suppressDialogs()
                    and not getattr(self, '_pending_envoy_prompt', False)):
                self.my.par.Envoyenable = False
                self._pending_envoy_prompt = True

    def _validateTrackedOperators(self) -> None:
        """Quiet upgrade-path validation of tracked operators.

        Non-destructive by contract: deletes nothing, clears no rows,
        never touches Status synchronously, and WRITES NO CONTENT --
        save_dirty=False keeps the deferred Update() to membership/tag
        reconciliation. The self-update's post-apply sweep used to flush
        every user-dirty externalization to disk (field 2026-08-19: an
        auto-update saved 4 toxes the user never chose to save).
        """
        table = self.Externalizations
        count = table.numRows - 1 if table else 0
        self.Log(
            f'{count} externalized operator(s) found -- '
            'validating tracked operators', 'INFO')
        self.my.par.externaltox = ''
        run(f"op('{self.my}').UpdateHandler(save_dirty=False)",
            delayFrames=10)

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
        choice = self._messageBox('Embody Warning',
            'Disable Embody?\nOnly files created by Embody will be deleted.\n'
            '(Non-Embody files in the folder will be preserved)',
            buttons=['No', 'Yes, keep Tags', 'Yes, remove Tags'])
        if choice == 1:
            self.Disable(self.ExternalizationsFolder, False)
        elif choice == 2:
            self.Disable(self.ExternalizationsFolder, True)

    def UpdateHandler(self, save_dirty: bool = True) -> None:
        """Enable/Update handler - main entry point for initialization.

        save_dirty=False (the upgrade-path validation) reconciles
        membership and tags without writing dirty content -- see
        _validateTrackedOperators."""
        if self.my.par.Status == 'Disabled':
            self.Log("Enabled", "SUCCESS")
            self.my.par.Status = 'Enabled'
            self.param_tracker.initializeTracking(self)
            
            # Create externalization folder (makedirs handles missing parents)
            # -- but never in TD's default location on a never-saved project.
            if self._projectSavedOnDisk():
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

        run(f"op('{self.my}').Update(save_dirty={save_dirty})", delayFrames=1)

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

    def Update(self, suppress_refresh: bool = False,
               save_dirty: bool = True) -> None:
        """Main update method - process additions, subtractions, and dirty ops.

        Args:
            suppress_refresh: If True, skip the delayed Refresh pulse. Used by
                onProjectPreSave() to prevent the continuity check from firing
                during the TDN strip/restore window.
            save_dirty: False = membership/tag reconciliation only; dirty
                COMPs are flagged, never written. The upgrade-path
                validation uses it -- an update must not save content the
                user did not choose to save (field 2026-08-19).
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
                self._setDirtyState(comp.path, 'Par')
                if save_dirty:
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

        # On a never-saved project every addition would be deferred by
        # handleAddition's save gate anyway; empty the list HERE so the
        # sweep report counts what actually landed, and say so once per
        # sweep instead of once per op.
        if additions and not self._projectSavedOnDisk():
            self.Log(f"Deferring {len(additions)} externalization"
                     f"{'s' if len(additions) > 1 else ''} until the "
                     "project is saved", "INFO")
            additions = []

        # Batch locked-content warnings across the whole sweep into ONE
        # combined dialog. A full-project externalization triggers one TDN
        # export per newly tagged COMP; without batching, each export with
        # locked TOP/CHOP/SOPs popped its own modal (field report: endless
        # popup loop). Flush in finally so a mid-sweep exception can never
        # leave the batch active and silently swallow later warnings.
        self.my.ext.TDN.BeginLockedWarnBatch()
        try:
            for oper in additions:
                self.handleAddition(oper)
            for oper in subtractions:
                self.handleSubtraction(oper)

            # Handle dirty COMPs (TOX + TDN)
            dirties = self.dirtyHandler(save_dirty)
        finally:
            self.my.ext.TDN.FlushLockedWarnBatch()

        # Report results
        self._reportResults(dirties, additions, subtractions)
        if not suppress_refresh:
            run(f"op('{self.my}').par.Refresh.pulse()", delayFrames=1)

        # Chain the first-run setup wizard AFTER init completes.
        # Verify() sets this flag; we consume it here so the wizard opens only
        # after deprecated-pattern and re-scan dialogs resolve. _openSetupWizard
        # respects _suppressDialogs (never opens during a test/save) and falls
        # back to the classic _promptEnvoy dialog if the wizard UI is absent.
        if getattr(self, '_pending_envoy_prompt', False):
            self._pending_envoy_prompt = False
            run(f"op('{self.my}').ext.Embody._openSetupWizard()", delayFrames=5)

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
        # Hoist the table: the Externalizations property EVALUATES a parameter
        # on every access, and this loop touched it per cell AND per loop bound.
        table = self.Externalizations
        if not table:
            return []

        family_str = 'COMP' if opFamily == COMP else 'DAT'
        has_strategy_col = 'strategy' in [
            self._cellVal(0, c, table=table)
            for c in range(table.numCols)
        ]
        ops = []

        for i in range(1, table.numRows):
            # Filter by strategy if requested
            if has_strategy_col and strategy:
                row_strategy = self._cellVal(i, 'strategy', table=table)
                if row_strategy != strategy:
                    continue
            elif not has_strategy_col:
                # Legacy table without strategy column -- skip TDN rows
                if self._cellVal(i, 'type', table=table) == 'tdn':
                    continue

            path = self._cellVal(i, 'path', table=table)
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

    def _retireVizBeforeWrite(self, path: str) -> None:
        """Retire Embot out of the COMP at `path` before its subtree is
        serialized (issue #86).

        EVERY call site that writes a COMP's whole subtree to disk goes through
        here -- Save (saveExternalTox), _setupCompForExternalization (the FIRST
        .tox for a newly tagged COMP), updateMovedOp (the rename re-save),
        ExportPortableTox (the live path) and _exportPortableViaCopy (before the
        staging copy is taken, since the copy snapshots whatever is standing in
        the live tree). One helper rather than five inline blocks so a sixth
        writer added later is an obvious omission rather than a silent leak:
        Embot's nine annotateCOMPs shipping inside a tracked or released .tox is
        a data defect, not a cosmetic one -- RestoreTOXComps then re-materialises
        them on every open.

        Subtree-scoped inside envoy_viz, so the many unrelated Save() calls
        dirtyHandler issues on an Update() never touch him. A no-op when Envoy is
        absent (it is optional in shipped Embody) and fully guarded -- retiring a
        viewing aid must never be able to fail a save."""
        try:
            _envoy = getattr(self.my.ext, 'Envoy', None)
            if _envoy is not None:
                _envoy._vizRetireForWrite(path)
        except Exception:
            pass

    def Save(self, opPath: str, allow_empty: bool = False) -> bool:
        """Save a TOX-strategy COMP and update tracking. Returns True
        only when the .tox was actually written.

        allow_empty mirrors SaveTDN: an AUTOMATIC save of an
        operator-empty COMP over a substantial existing .tox is the
        transiently-emptied-shell shape (the TOX-side twin of the TDN
        data loss, review finding 2026-08-12). A .tox cannot be parsed
        for content, so the on-disk test is a size heuristic: an empty
        COMP's .tox is ~1-2 KB of shell + parameters, so an existing
        file over 4 KB is treated as content worth protecting. The
        explicit manager Save passes allow_empty=True.
        """
        if self._performMode:
            return False
        try:
            oper = op(opPath)
            if not oper or oper.family != 'COMP':
                self.Log(f"Save() requires a COMP, got {oper.family if oper else 'None'}: {opPath}", "ERROR")
                return False
            if not allow_empty:
                try:
                    if not any(c.type != 'annotate' for c in oper.children):
                        rel_tox = self.getExternalPath(oper)
                        if rel_tox:
                            existing = self.buildAbsolutePath(rel_tox)
                            if (existing.is_file()
                                    and existing.stat().st_size > 4096):
                                self.Log(
                                    f'REFUSED auto-save of {opPath}: the '
                                    f'COMP is empty but its .tox on disk '
                                    f'is {existing.stat().st_size} bytes '
                                    f'-- overwriting would destroy the '
                                    f'only good copy. If the empty state '
                                    f'is intentional, use the manager '
                                    f'Save button.', 'WARNING')
                                return False
                except Exception:
                    pass
            oper.par.enableexternaltox = True

            # Update build info on the OPERATOR now; the matching table cells
            # are collected and written ONCE after the .tox is actually on
            # disk. Four separate cell writes here cost four full rewrites of
            # the syncfile-backed .tsv (~15ms each, measured), and writing
            # them before saveExternalTox() also advanced the table's build
            # past what was on disk if the save then threw.
            row_changes = {}
            if hasattr(oper.par, 'Build'):
                new_build = oper.par.Build.val + 1
                oper.par.Build = new_build
                row_changes['build'] = str(new_build)

            if hasattr(oper.par, 'Date'):
                oper.par.Date.val = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            if hasattr(oper.par, 'Touchbuild'):
                oper.par.Touchbuild = app.build
                row_changes['touch_build'] = app.build

            # Issue #86: saveExternalTox() would otherwise write Embot's
            # annotation parts into the .tox -- see _retireVizBeforeWrite.
            self._retireVizBeforeWrite(opPath)

            oper.saveExternalTox()

            # Update timestamp
            if hasattr(oper.par, 'externalTimeStamp') and oper.externalTimeStamp != 0:
                utc_time = datetime.utcfromtimestamp(oper.externalTimeStamp / 10000000 - 11644473600)
                timestamp = utc_time.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            self.param_tracker.updateParamStore(oper)
            # build/touch_build (from above) + timestamp + dirty in ONE write.
            row_changes['timestamp'] = timestamp
            self._setDirtyState(opPath, '')
            # Position/color merges into the SAME write -- see _positionCells.
            row_changes.update(self._positionCells(oper))
            self._updateRowCells(opPath, row_changes, strategy='tox')

            self.Log(f"Saved {opPath}", "SUCCESS")
            return True
        except Exception as e:
            self.Log("Save failed", "ERROR", str(e))
            return False

    def SaveTDN(self, opPath: str, bump_build: bool = True,
                allow_empty: bool = False) -> bool:
        """Save a TDN-strategy COMP by re-exporting its .tdn file.
        Returns True only when the file was actually written -- callers
        (dirtyHandler's Saved-N tally, the MCP save surface) must not
        report a refusal or failure as a save (review finding).

        bump_build=False re-exports WITHOUT advancing par.Build. The
        post-save version sync needs that: the release manifest records
        par.Build before the sync runs, so a second bump would leave the
        manifest one behind the .tdn -- a smaller copy of the very drift
        the sync exists to remove. Checkpoint() already skips the bump
        for the same class of reason.

        allow_empty=False (every automatic caller) refuses to overwrite
        a non-empty .tdn on disk from an operator-empty COMP -- the
        signature of a transiently-emptied shell about to destroy the
        only good copy (field data loss, 2026-08-12). The explicit
        manager Save passes True: a deliberately emptied COMP may save.
        A refusal re-baselines the fingerprint so the dirty-sweep
        converges (warn once, not forever) while any LATER real edit
        still reads dirty.
        """
        if self._performMode:
            return False
        if not self._tdnEnabled():
            self.Log(f'TDN disabled -- skipping SaveTDN for {opPath}', 'INFO')
            return False
        try:
            oper = op(opPath)
            if not oper:
                self.Log(f"Operator not found: {opPath}", "ERROR")
                return False

            # Get the TDN file path from the table
            rel_path = self._getStrategyFilePath(opPath, 'tdn')
            if not rel_path:
                self.Log(f"No TDN entry found for {opPath}", "ERROR")
                return False

            if not allow_empty and self._refusesEmptyTDNOverwrite(
                    oper, str(self.buildAbsolutePath(rel_path))):
                self._storeTDNFingerprint(oper)
                return False

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

            # Update build info. The table's `build` MUST be written before the
            # export: TDNExt._getBuildNumber treats the TSV as source of truth
            # and the exporter stamps the .tdn header from it, so skipping this
            # freezes the recorded build while par.Build advances (caught in
            # testing -- par.Build 8, table build 1).
            #
            # touch_build is deliberately NOT written here: _trackTDNExport
            # records it after a successful export in the fuller
            # '099.2025.33070' form, so writing app.build here only guaranteed
            # a second differing value and an extra full rewrite of the
            # syncfile-backed .tsv (~15ms measured -- see _updateRowCells).
            if bump_build and hasattr(oper.par, 'Build'):
                new_build = oper.par.Build.val + 1
                oper.par.Build = new_build
                self._updateRowCells(opPath, {'build': str(new_build)},
                                     strategy='tdn')

            if hasattr(oper.par, 'Date'):
                oper.par.Date.val = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            if hasattr(oper.par, 'Touchbuild'):
                oper.par.Touchbuild = app.build

            # Export TDN -- protect .tdn files belonging to OTHER tracked
            # TDN COMPs so the stale-file cleanup doesn't delete them.
            abs_path = str(self.buildAbsolutePath(rel_path))
            protected = self._getAllTrackedTDNFiles(exclude_path=opPath)
            result = self.my.ext.TDN.ExportNetwork(
                root_path=opPath, output_file=abs_path,
                cleanup_protected=protected)

            if result.get('success'):
                timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                self.param_tracker.updateParamStore(oper)
                # timestamp + position in ONE row write; dirty is runtime.
                self._setDirtyState(opPath, '')
                tdn_changes = {'timestamp': timestamp}
                tdn_changes.update(self._positionCells(oper))
                self._updateRowCells(opPath, tdn_changes, strategy='tdn')
                # Snapshot the network structure so _isTDNDirty returns False
                self._storeTDNFingerprint(oper)
                self.Log(f"Exported TDN for {opPath}", "SUCCESS")
                return True
            self.Log(f"TDN export failed for {opPath}: {result.get('error')}", "ERROR")
            return False
        except Exception as e:
            self.Log(f"SaveTDN failed for {opPath}", "ERROR", str(e))
            return False

    def Checkpoint(self, opPath: str) -> bool:
        """Frame-cheap SYNCHRONOUS auto-save checkpoint of one TDN COMP.

        Re-exports with stale-cleanup skipped (the ~700ms rglob is the
        dominant save cost; a single-COMP checkpoint orphans nothing) --
        ~6ms typical; the ~40ms fingerprint re-baseline defers one frame.
        Gated on Perform Mode + the save window (table mutation during
        strip = fatal crash); caller owns the perf-gate. No Build bump
        (a checkpoint must be diff-stable vs the next Ctrl+S).
        """
        if self._performMode:
            return False
        if self.my.fetch('_suppress_dialogs', False, search=False):
            return False  # save window open -- never mutate the table now
        if not self._tdnEnabled():
            return False
        try:
            oper = op(opPath)
            if not oper:
                return False
            rel_path = self._getStrategyFilePath(opPath, 'tdn')
            if not rel_path:
                return False
            abs_path = str(self.buildAbsolutePath(rel_path))
            # A checkpoint is always automatic -- never let it overwrite
            # a non-empty .tdn from a transiently-emptied shell.
            if self._refusesEmptyTDNOverwrite(oper, abs_path):
                return False
            result = self.my.ext.TDN.ExportNetwork(
                root_path=opPath, output_file=abs_path, skip_cleanup=True)
            if not result.get('success'):
                self.Log(f'Checkpoint export failed for {opPath}: '
                         f'{result.get("error")}', 'WARNING')
                return False
            # Mark clean + stamp now; defer the heavy fingerprint re-baseline off
            # this frame. Without re-baselining, _isTDNDirty reads false-dirty
            # forever and the next Ctrl+S re-exports an already-current COMP.
            # ONE row write for all of it (see _updateRowCells): a checkpoint
            # fires on the autosave drain, so its table churn is the most
            # frequent of any save path. Position/color rides along because
            # recovery restores the boundary's own node_x/y/color from the
            # TABLE (not the .tdn) -- a moved or recolored COMP would otherwise
            # come back at stale coordinates.
            self._setDirtyState(opPath, '')
            cp_changes = {
                'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
            }
            cp_changes.update(self._positionCells(oper))
            self._updateRowCells(opPath, cp_changes, strategy='tdn')
            self._setAutosaveStatus('Saved ' + self._autosaveClock())
            # delayFrames=2 staggers the ~40ms re-baseline off F+1, where the
            # drain schedules the NEXT root's checkpoint -- so they never co-fire.
            run(f"op({self.my.path!r}).ext.Embody._reBaselineCheckpoint({opPath!r})",
                fromOP=self.my, delayFrames=2)
            return True
        except Exception as e:
            self.Log(f'Checkpoint failed for {opPath}', 'WARNING', str(e))
            return False

    def _refusesEmptyTDNOverwrite(self, oper, abs_path: str) -> bool:
        """True when an AUTOMATIC export must not overwrite this .tdn.

        Empty COMP over a non-empty on-disk .tdn = transiently-emptied
        shell about to destroy the only good copy (field loss,
        2026-08-12). Automatic writers refuse loudly; the manager's
        explicit Save still passes allow_empty=True. Polarity on doubt:
        missing file allows (nothing to destroy), unparseable file
        refuses (hand-repairable bytes are most valuable exactly then).
        Annotate children don't count as content. Warned once per
        (path, mtime, size).
        """
        try:
            if any(c.type != 'annotate' for c in oper.children):
                return False
            from pathlib import Path
            existing = Path(abs_path)
            if not existing.is_file():
                return False
            stat = existing.stat()
            cache_key = (abs_path, stat.st_mtime_ns, stat.st_size)
            cached = self._empty_guard_cache.get(abs_path)
            if cached is not None and cached[0] == cache_key:
                return cached[1]
            refused = False
            detail = ''
            try:
                doc = self.my.ext.TDN.tdn_load(
                    existing.read_text(encoding='utf-8'))
                if isinstance(doc, dict):
                    refused = bool(doc.get('operators')
                                   or doc.get('annotations'))
                else:
                    refused = bool(doc)
                if refused:
                    detail = 'holds a non-empty network'
            except Exception:
                if stat.st_size > 0:
                    refused = True
                    detail = ('exists but cannot be parsed -- its bytes '
                              'may still be hand-recoverable')
            self._empty_guard_cache[abs_path] = (cache_key, refused)
            if not refused:
                return False
        except Exception:
            return False
        self.Log(
            f'REFUSED auto-export of {oper.path}: the COMP is empty but '
            f'its .tdn on disk {detail} -- overwriting would destroy '
            f'the only good copy. If the empty state is intentional, '
            f'use the manager Save button.', 'WARNING')
        return True

    def _reBaselineCheckpoint(self, opPath: str) -> None:
        """Deferred dirty-detection re-baseline after a checkpoint.

        Runs the ~40ms fingerprint + param-store snapshot OFF the export frame so
        _isTDNDirty reads clean. This is the HEAVIEST cost the feature adds, so it
        is gated: identity (a stale instance after reinit is a no-op), Perform
        Mode + save window (skip), and the perf-gate (reschedule under FPS danger
        rather than pile ~40ms onto a hot frame -- the .tdn is already durable, so
        only the dirty badge waits)."""
        if self.my.ext.Embody is not self:
            return  # superseded instance (reinit)
        if opPath in self._pending_checkpoint_roots:
            # Re-touched since this checkpoint -- the .tdn on disk is now STALE, so
            # do NOT baseline the (newer) live state as clean. The pending
            # re-checkpoint will write the new .tdn and baseline it then.
            return
        if self._performMode or self.my.fetch('_suppress_dialogs', False, search=False):
            return
        if not self._autosavePerfOk():
            run(f"op({self.my.path!r}).ext.Embody._reBaselineCheckpoint({opPath!r})",
                fromOP=self.my, delayFrames=15)
            return
        oper = op(opPath)
        if not oper:
            return
        try:
            self.param_tracker.updateParamStore(oper)
            self._storeTDNFingerprint(oper)
        except Exception as e:
            self.Log(f'Checkpoint re-baseline failed for {opPath}', 'DEBUG', str(e))

    # --- Auto-save / crash checkpoint engine (event-armed idle-settle drain) ---
    _AUTOSAVE_IDLE_SECONDS = 1.0    # checkpoint this long after the last MCP mutation
    _AUTOSAVE_POLL_FRAMES = 12      # re-check cadence while waiting to settle
    _AUTOSAVE_FPS_FLOOR_FRAC = 0.9  # perf-gate: defer if fps < this * target

    def _autosaveEnabled(self) -> bool:
        """True if the Autosave toggle is on (default on until the param exists)."""
        p = getattr(self.my.par, 'Autosave', None)
        return bool(p.eval()) if p is not None else True

    def NoteCheckpointTouch(self, op_path: str) -> None:
        """Record (best-effort) that op_path was mutated via MCP, and queue an idle
        checkpoint of its nearest tracked TDN boundary. HOT PATH: cheap, never
        raises. Walks the PATH STRING up to the boundary so it survives a
        just-deleted op (delete_op leaves no live op to resolve)."""
        try:
            if not op_path or self._performMode or not self._autosaveEnabled():
                return
            parts = op_path.rstrip('/').split('/')
            while len(parts) > 1:
                cand = '/'.join(parts)
                m = self._findExternalizedComp(cand)
                if m and m[1] == 'tdn':
                    self._queueCheckpoint(cand)
                    return
                parts.pop()
        except Exception:
            pass

    _COARSE_SWEEP_CAP = 60   # roots examined in one coarse sweep (bounded work)

    def NoteCoarseCheckpointTouch(self) -> None:
        """Arm a checkpoint after an op that could touch ANY tracked root.

        execute_python names no path, so it used to arm nothing -- whole
        agent sessions checkpointed nothing while the readout looked fine
        (2h46m stale, 2026-08-09). ARMS only; the settle-drain discovers
        which roots actually changed, once, after the burst.
        """
        try:
            import time
            if self._performMode or not self._autosaveEnabled():
                return
            self._coarse_checkpoint_due = True
            self._last_checkpoint_activity = time.monotonic()
            if not self._autosave_armed:
                self._armAutosaveDrain()
        except Exception:
            pass

    def _queueDirtyTDNRoots(self) -> int:
        """Expand a coarse arm into the roots that ACTUALLY changed.

        Unions the externalizations table's TDN rows with the fingerprint
        baselines, because `_getTDNStrategyComps` deliberately omits Embody and
        its descendants (reconstructing inside Embody is self-destruction) while
        the baselines DO cover them -- and the Embody COMP is exactly where an
        agent editing the manager UI does its work. Bounded by _COARSE_SWEEP_CAP
        so one sweep can never become the frame cost it exists to avoid."""
        queued = 0
        try:
            tdn_paths = self._getTDNPaths()
            roots = set(tdn_paths) | set(self._tdn_fingerprints.keys())
            for path in sorted(roots)[:self._COARSE_SWEEP_CAP]:
                if path in self._pending_checkpoint_roots:
                    continue
                comp = op(path)
                if comp is None or not comp.valid:
                    continue
                if self._isTDNDirty(comp, tdn_paths=tdn_paths):
                    self._pending_checkpoint_roots.add(path)
                    queued += 1
        except Exception:
            pass
        return queued

    def FlushPendingCheckpoints(self) -> int:
        """Write ALREADY-QUEUED checkpoints now, before risky code runs.

        Same ordering argument as _preRiskyCheckpoint: a root queued by an
        earlier tool sits unwritten for up to _AUTOSAVE_IDLE_SECONDS, and
        execute_python can crash TD inside that window and take it with it.
        This deliberately does NOT sweep for new dirt -- discovery is the
        post-arm's job, debounced -- so in the steady state it is an
        empty-set check costing nothing."""
        written = 0
        try:
            if self._performMode or not self._autosaveEnabled():
                return 0
            if self.my.fetch('_suppress_dialogs', False, search=False):
                return 0
            for path in list(self._pending_checkpoint_roots):
                if self.Checkpoint(path):
                    self._pending_checkpoint_roots.discard(path)
                    written += 1
        except Exception:
            pass
        return written

    def _queueCheckpoint(self, comp_path: str) -> None:
        import time
        self._pending_checkpoint_roots.add(comp_path)
        self._last_checkpoint_activity = time.monotonic()
        if not self._autosave_armed:
            self._armAutosaveDrain()

    def _armAutosaveDrain(self) -> None:
        self._autosave_armed = True
        self._autosave_gen += 1
        run(f"op({self.my.path!r}).ext.Embody._autosaveDrain({self._autosave_gen})",
            fromOP=self.my, delayFrames=self._AUTOSAVE_POLL_FRAMES)

    def _autosaveDrain(self, gen: int) -> None:
        """Settle-debounced one-COMP-per-frame checkpoint drain (main thread).

        Fires ~_AUTOSAVE_IDLE_SECONDS after the last MCP mutation, then writes one
        pending root per frame. Self-stops when the set drains (no perpetual poll).
        Defers (re-arms) on: not-settled / save window open / perf danger. Holds
        the set (stops, resumes later) when disabled or in Perform Mode."""
        import time
        # reinit guard (a stale instance) + superseded-gen guard (collapses re-arms)
        if self.my.ext.Embody is not self or gen != self._autosave_gen:
            return
        self._autosave_armed = False
        if not self._pending_checkpoint_roots and not self._coarse_checkpoint_due:
            return
        if not self._autosaveEnabled() or self._performMode:
            self._setAutosaveStatus(
                'Disabled' if not self._autosaveEnabled()
                else 'Bypassed (Perform Mode)')
            return  # hold the set; Perform-Mode exit / re-enable re-arms it
        if time.monotonic() - self._last_checkpoint_activity < self._AUTOSAVE_IDLE_SECONDS:
            self._armAutosaveDrain()  # not settled -- wait
            return
        if self.my.fetch('_suppress_dialogs', False, search=False):
            self._armAutosaveDrain()  # save window -- table mutation now is fatal
            return
        if not self._autosavePerfOk():
            self._armAutosaveDrain()  # perf danger -- don't pile onto a hot frame
            return
        # Coarse arm (execute_python): discover WHICH roots changed, now that we
        # are settled and off a hot frame. Cleared before the sweep so a failure
        # cannot re-sweep forever; a later touch simply re-arms.
        if self._coarse_checkpoint_due:
            self._coarse_checkpoint_due = False
            self._queueDirtyTDNRoots()
            if not self._pending_checkpoint_roots:
                return   # nothing actually changed -- the common case
        try:
            root = self._pending_checkpoint_roots.pop()
        except KeyError:
            return
        if not self.Checkpoint(root):
            # Export/write failed (rare; logged in Checkpoint). We don't re-add
            # here -- that would tight-loop on a persistently-failing COMP; the
            # next edit to this COMP re-queues it.
            self.Log(f'Autosave: checkpoint of {root} did not complete', 'DEBUG')
        if self._pending_checkpoint_roots:  # more roots -- one per frame
            self._autosave_armed = True
            self._autosave_gen += 1
            run(f"op({self.my.path!r}).ext.Embody._autosaveDrain({self._autosave_gen})",
                fromOP=self.my, delayFrames=1)

    def _autosavePerfOk(self) -> bool:
        """Best-effort perf-gate: False only if fps is clearly in the danger zone."""
        try:
            perf = self.my.op('_envoy_perform')
            if not perf or 'fps' not in [c.name for c in perf.chans]:
                return True
            fps = float(perf['fps'].eval())
            target = float(project.cookRate or 60)
            return fps >= self._AUTOSAVE_FPS_FLOOR_FRAC * target
        except Exception:
            return True

    def _autosaveClock(self) -> str:
        """The auto-save time, in the timezone the user reads in.

        Honours the EXISTING Localtimestamps toggle (default on) rather
        than inventing a second preference: the externalizations table
        already converts its UTC stamps that way, and a readout that
        says 14:53 while the table beside it says 07:53 is the same
        value reported two ways. The tz abbreviation is shortened the
        way list_callbacks does, because macOS returns full names like
        'Pacific Daylight Time'.
        """
        try:
            local = self.my.par.Localtimestamps.eval()
        except Exception:
            local = True
        if not local:
            return datetime.utcnow().strftime('%H:%M:%S') + ' UTC'
        try:
            now = datetime.now().astimezone()
            abbr = now.strftime('%Z')
            if len(abbr) > 5:
                abbr = ''.join(word[0] for word in abbr.split())
            return now.strftime('%H:%M:%S') + (' ' + abbr if abbr else '')
        except Exception:
            return datetime.utcnow().strftime('%H:%M:%S') + ' UTC'

    def _setAutosaveStatus(self, msg: str) -> None:
        """Set the read-only Autosavestatus readout (no-op if the param is absent)."""
        p = getattr(self.my.par, 'Autosavestatus', None)
        if p is not None:
            try:
                p.val = msg
            except Exception:
                pass

    def SeedAutosaveStatus(self) -> str:
        """Seed the last-write readout from the table instead of 'Idle'.

        'Idle' is right to SHIP (release scrub) and wrong to display --
        the readout's one job is how long ago work reached disk, and the
        table's newest tracked write is the answer. Seeds only a resting
        value (a real checkpoint outranks it; Disabled/Bypassed/failed
        left alone). Date KEPT deliberately: folding a weeks-old stamp
        into a 24h clock reported '1h ago' on a seven-week-old project.
        """
        try:
            p = getattr(self.my.par, 'Autosavestatus', None)
            if p is None:
                return ''
            current = str(p.eval() or '').strip()
            if current and current.lower() not in ('idle', 'none', 'never'):
                return current      # a real state -- never overwrite it
            table = self.Externalizations
            if table is None:
                return current
            # _cellVal, not raw table[r, c].val: a partial externalize
            # cascade can leave short rows, and None.val is exactly the
            # issue-#21 crash the guard exists for.
            headers = [self._cellVal(0, c) for c in range(table.numCols)]
            if 'timestamp' not in headers:
                return current
            col = headers.index('timestamp')
            newest = ''
            for r in range(1, table.numRows):
                stamp = self._cellVal(r, col).strip()
                # Lexicographic == chronological ONLY because every writer
                # in this file uses one fixed-width format with a constant
                # suffix ('%Y-%m-%d %H:%M:%S UTC'). If that ever varies,
                # this needs strptime -- as cleanupDuplicateRows already
                # does on this very column.
                if stamp > newest:
                    newest = stamp
            if not newest:
                return current
            self._setAutosaveStatus('Saved ' + newest)
            # The parameter-execute DAT that normally redraws the readout is
            # not watching yet at frame 0, so this write has to announce
            # itself or the seeded row is not drawn until the next event.
            self._republishStatusPanel()
            return str(p.eval() or '')
        except Exception:
            return ''

    def _preRiskyCheckpoint(self, operation: str, params: dict) -> None:
        """Synchronously checkpoint the touched TDN root BEFORE a destructive
        delete (delete_op of a CHILD inside a tracked COMP) so an agent-induced
        crash DURING it loses nothing since it. ~6ms one COMP; gated like
        Checkpoint. Called from EnvoyExt _execute_operation before the handler.

        NOT for import_network: its .tdn is the user's source-of-truth being
        reloaded, so checkpointing the live state over it would corrupt the edit.
        And NOT when the op being deleted IS the boundary itself (it is going away
        and _delete_op purges it -- the checkpoint would be discarded). Best-effort."""
        try:
            if self._performMode or not self._autosaveEnabled():
                return
            if self.my.fetch('_suppress_dialogs', False, search=False):
                return
            path = params.get('op_path')
            if not path:
                return
            norm = path.rstrip('/')
            parts = norm.split('/')
            first = True
            while len(parts) > 1:
                cand = '/'.join(parts)
                m = self._findExternalizedComp(cand)
                if m and m[1] == 'tdn':
                    if first and cand == norm:
                        return  # deleting the boundary itself -- nothing to protect
                    if self.Checkpoint(cand):
                        self._pending_checkpoint_roots.discard(cand)
                    return
                parts.pop()
                first = False
        except Exception:
            pass

    def _purgeExternalizationTracking(self, op_path: str) -> None:
        """Remove tracking row(s) + externalized file(s) for op_path and any
        tracked DESCENDANT -- ANY strategy -- called from delete_op BEFORE the
        op is destroyed.

        Without this, deleting a tracked TDN COMP leaves its row + .tdn on disk
        until the next continuity sweep; a crash in that window would let
        export-mode autosave recovery RESURRECT the just-deleted COMP on reopen
        (it is tsv-driven, so a lingering row = a rebuild). Removing the row up
        front makes the deletion durable. `_removeTDNStrategy` removes the row
        synchronously (the safety hinge) and deletes the .tdn shortly after.

        Non-TDN strategies (py/tox/dat/json/...) previously were not purged at
        all: delete_op left their row until a Refresh sweep reclaimed it and
        left the externalized file on disk forever (issue #57 follow-up,
        2026-07-16). They now get the same treatment: row removed up front,
        file deleted shortly after (mirroring the TDN delete_file=True
        semantics -- an explicit delete_op is intent to remove the entity).

        Best-effort; never raises into the delete path. Drops the paths from
        the pending checkpoint queue too."""
        try:
            # Never mutate the externalizations table during the save window
            # (table mutation during onProjectPreSave/strip is a fatal crash).
            # A delete landing mid-save just leaves the row for the post-save
            # continuity sweep to reclaim -- recovery is tsv-driven so a brief
            # lingering row is benign.
            if self.my.fetch('_suppress_dialogs', False, search=False):
                return
            table = self.Externalizations
            if not table or table[0, 'strategy'] is None:
                return
            prefix = op_path.rstrip('/') + '/'
            tdn_paths = []
            other_paths = []
            for i in range(1, table.numRows):
                p = self._cellVal(i, 'path')
                if p != op_path and not p.startswith(prefix):
                    continue
                if self._cellVal(i, 'strategy') == 'tdn':
                    tdn_paths.append(p)
                else:
                    other_paths.append((p, self._cellVal(i, 'rel_file_path')))
            for p in tdn_paths:
                self._pending_checkpoint_roots.discard(p)
                self._removeTDNStrategy(p, delete_file=True)
            for p, rel_path in other_paths:
                # Row indices shift as rows are deleted -- re-resolve by path.
                removed = False
                for i in range(table.numRows - 1, 0, -1):
                    if self._cellVal(i, 'path') == p:
                        table.deleteRow(i)
                        removed = True
                        break
                if removed:
                    self.Log(
                        f'Removed externalization tracking for deleted op {p}',
                        'INFO')
                if not rel_path:
                    continue
                # Shared-file guards, mirroring RemoveListerRow: never unlink
                # a file that a clone-tagged op owns or that another live op
                # still references (two ops CAN share one rel_file_path).
                # Both checks must run NOW -- the op still exists (purge runs
                # before target.destroy()).
                normalized = self.normalizePath(rel_path)
                oper = op(p)
                is_clone = bool(oper) and 'clone' in oper.tags
                other_refs = self._checkFileReferences(p, normalized)
                if is_clone or other_refs:
                    self.Log(
                        f"Preserved file '{normalized}' (still in use)",
                        'INFO')
                    continue
                full_path = self.buildAbsolutePath(normalized).resolve()
                def _delete(fp=full_path, rp=rel_path, opp=p):
                    try:
                        if fp.is_file():
                            fp.unlink()
                            self.Log(
                                f'Removed externalized file for deleted '
                                f'op {opp} ({rp})', 'SUCCESS')
                    except Exception as e:
                        self.Log(
                            f'Error removing externalized file: {e}',
                            'ERROR')
                run(_delete, delayFrames=5)
        except Exception as e:
            self.Log(f'_purgeExternalizationTracking failed for {op_path}: {e}',
                     'DEBUG')

    # A-50: custom pars whose VALUES are runtime status, never authored
    # state. One registry, two consumers: TDN export writes the RESTING
    # value ('value: Testing' once shipped in a release commit) and
    # ExportPortableTox resets them around the save. Keyed by global OP
    # shortcut so user pars sharing a name are untouched.
    # RESTING, never par.default: Status's default '' bricks the enable
    # state-machine on cached installs. None = reset to par defaults,
    # the only valid entry for a SEQUENCE name (block count preserved).
    # Single pars only (tuplet base names skipped by every consumer).
    # The registry is the LAST word in every export mode -- a
    # pre_release hook cannot ship a session value for a registered par.
    _TRANSIENT_STATUS_PARS = {
        'Embody': {
            'Status': 'Disabled',        # 'Testing'/'Enabled' at runtime
            'Autosavestatus': 'Idle',    # 'Saved <time> UTC'/'Bypassed'
            'Envoystatus': 'Disabled',   # 'Running on port N'/'Perform Mode'
            'Updatestatus': 'Disabled',  # updater state beyond its rest
            # The single Convoy readout. It merges what used to be two
            # fields (node registration + host-app state); the separate
            # Convoyid and Convoyhoststatus rows were removed because a
            # truncated node hash, a truncated host hash and a process id
            # are not things a user can act on. It still MUST be scrubbed:
            # Embody.tdn is the tracked source ExportPortableTox builds
            # released .tox files from, and a live readout there would ship
            # one machine's state to every download (the A-50 leak class,
            # value: Testing, v6.0.169).
            'Convoystatus': 'Disabled',  # 'Connected'/'No Convoy host app'
            # CONSENT, not state: a released .tox that ships Convoyenable On
            # would enable Convoy -- and its LAN listener -- on every machine
            # that installs it, without anyone opting in. The user's own
            # choice is NOT lost by scrubbing: Convoyenable is in the
            # config.json prefs whitelist above, which is what restores it
            # across restarts and upgrades. Reset to its default (Off).
            'Convoyenable': None,
            # The node's display name. It is auto-derived per machine at
            # runtime (hostname / .toe stem), so a baked value ships one
            # developer's COMPUTER NAME to every download -- the A-50 leak
            # class. Rests empty; ConvoyExt refills it on load, once the
            # project is saved (an early fill baked NewProject.1 forever).
            'Convoynodename': None,
            # Read-only network status rows. Sequence registration scrubs
            # every runtime-populated block back to its template defaults
            # while preserving the block count, so another machine's names,
            # addresses and presence never bake into a TDN or release tox.
            'Convoynodes': None,
            # A dev dialog preference that SHIPPED On in v6.0.246 (every
            # download saw TD's built-in pages instead of the POPX
            # filter). Not in the config.json whitelist, so the scrub
            # costs the developer nothing and closes the leak. Reset Off.
            'Showbuiltinpars': None,
            # Same leak class, opposite direction: v6.0.251 shipped
            # Clipboardautopaste Off (dev machine quieted at bake time),
            # killing the "Embody it" copy flow on fresh installs
            # (2026-08-18). Reset On; a deliberate user Off persists via
            # the _PERSISTED_PARAMS whitelist.
            'Clipboardautopaste': None,
        },
    }

    # TDN-only companion registry: machine-written metadata whose values
    # churn per save (the About-page stamp the release machinery rewrites).
    # The .tdn omits their value key -- definitions and help text stay in
    # the diffable record, and ExportPortableTox does NOT touch them (a
    # released .tox must carry its real Build/Date).
    _TDN_VALUE_OMIT_PARS = {
        'Embody': frozenset({'Build', 'Date'}),
    }

    @staticmethod
    def _registryShortcut(comp) -> str:
        """The comp's global-OP-shortcut par VALUE (the registry key), or ''."""
        try:
            return str(comp.par.opshortcut.eval())
        except Exception:
            return ''

    def _transientParNames(self, comp) -> dict:
        """Registered {par_name: resting_value} for `comp`, else empty dict.

        Scoped by the comp's global OP shortcut -- the registry key -- so
        the scrub can never reach a user parameter that happens to share
        a registered name.
        """
        shortcut = self._registryShortcut(comp)
        if not shortcut:
            return {}
        return self._TRANSIENT_STATUS_PARS.get(shortcut, {})

    def _tdnValueOmitNames(self, comp) -> frozenset:
        """Registered TDN-value-omit par names for `comp`, else empty."""
        shortcut = self._registryShortcut(comp)
        if not shortcut:
            return frozenset()
        return self._TDN_VALUE_OMIT_PARS.get(shortcut, frozenset())

    def _scrubTransientPars(self, root) -> list:
        """Reset registered runtime-status pars to their RESTING values on
        `root` and every descendant COMP; return a [(par, value)] snapshot
        so a live-mode export can hand the session its readouts back.

        Constant-mode pars only -- an expression or bind carries no baked
        value to leak, and resetting it would destroy the reference. A
        registered sequence (resting None) resets per-block values to
        defaults but NEVER touches numBlocks. This function never raises:
        whatever was scrubbed before any failure is always returned, so
        the caller's restore can undo partial progress.
        """
        snapshot = []
        try:
            comps = [root] + root.findChildren(type=COMP)
        except Exception:
            comps = [root]
        try:
            for comp in comps:
                for name, resting in self._transientParNames(comp).items():
                    # Per-name containment: a raise must never escape with
                    # pars already reset but the snapshot unreturned.
                    try:
                        # Sequence lookup via enumeration, not attribute
                        # access -- TDNExt documents the attribute accessor
                        # as unreliable (POPs); enumeration is the
                        # discovery path the exporter itself trusts.
                        seq = None
                        try:
                            for s in comp.seq:
                                if s is not None and s.name == name:
                                    seq = s
                                    break
                        except Exception:
                            seq = None
                        if seq is not None:
                            # Iterating a SequenceBlock yields tuple-like
                            # ParGroups (their .mode is a TUPLE -- comparing
                            # it to ParMode.CONSTANT silently never
                            # matches); unwrap to the individual Pars.
                            for block in seq.blocks:
                                for group in block:
                                    for p in group:
                                        if (p.mode == ParMode.CONSTANT
                                                and p.val != p.default):
                                            snapshot.append((p, p.val))
                                            p.val = p.default
                            continue
                        p = getattr(comp.par, name, None)
                        if p is None:
                            continue
                        target_val = (p.default if resting is None
                                      else resting)
                        if p.mode == ParMode.CONSTANT and p.val != target_val:
                            snapshot.append((p, p.val))
                            p.val = target_val
                    except Exception as e:
                        self.Log(
                            f'Transient scrub skipped {comp.path}.{name}: '
                            f'{e}', 'WARNING')
        except Exception as e:
            # Outer containment: even a failure between names/comps must
            # hand back the partial snapshot for restore.
            self.Log(f'Transient scrub aborted mid-walk: {e}', 'WARNING')
        return snapshot

    def _scrubLogBuffers(self, root) -> list:
        """Empty every log-display DAT on `root` and its descendants; return a
        [(dat, text)] snapshot so the live session gets its log back.

        The Log() ring writes to a FIFO DAT for on-screen display, and a DAT's
        rows are saved WITH the component -- so an exported .tox carried this
        machine's session log (host/convoy identifiers, local paths) into every
        download. Same A-50 rule as the status pars, applied to content rather
        than values. Never raises: whatever was cleared before a failure is
        still returned so the caller's restore can undo partial progress.
        """
        snapshot = []
        try:
            comps = [root] + root.findChildren(type=COMP)
        except Exception:
            comps = [root]
        for comp in comps:
            # Scoped by op shortcut exactly like _transientParNames, so a
            # user's own FIFO DAT is never touched.
            if not self._transientParNames(comp):
                continue
            for dat in comp.findChildren(type=DAT, depth=1):
                try:
                    if dat.name != 'fifo1' or not dat.numRows:
                        continue
                    snapshot.append((dat, dat.text))
                    dat.clear()
                except Exception as e:
                    self.Log(f'Log scrub skipped {comp.path}: {e}', 'WARNING')
        return snapshot

    def _restoreLogBuffers(self, snapshot) -> None:
        """Put the display log rows back (always runs, success or failure)."""
        for dat, text in snapshot:
            try:
                dat.text = text
            except Exception:
                pass

    def _restoreTransientPars(self, snapshot) -> None:
        """Reapply the values _scrubTransientPars captured (always runs,
        success or failure -- a live session must get its readouts back)."""
        for p, val in snapshot:
            try:
                p.val = val
            except Exception:
                pass

    def ExportPortableTox(self, target: 'OP' = None,
                          save_path: Optional[str] = None,
                          run_hooks: bool = True,
                          hook_mode: str = 'copy') -> bool:
        """Export a self-contained .tox: file refs + Embody tags stripped,
        saved, then restored. Warns on non-portable absolute paths.

        Release hooks (issue #74): direct-child Text DATs 'pre_release' /
        'post_release'. Default hook_mode='copy' (Private Investigator
        model): target copied into cooking-disabled /sys/quiet,
        pre_release runs ON THE COPY (shapes the artifact, never the
        live comp), hook DATs deleted from the copy (never ship), then
        post_release runs on the ORIGINAL. Hooks get args[0]=save path;
        post_release args[1]=success. A pre_release raise aborts and
        keeps the copy as *_release_failed; once pre_release completes,
        post_release always runs, even on a failed save. The copy is
        neutralized first (tags removed, file-sync off; extensions do
        NOT init there -- use hook_mode='live' for extension logic).
        'live' = hooks on the live target, mutations persist, hook DATs
        ship dormant; forced for targets that ARE the Embody COMP (a
        copied live Embody would boot a second Envoy); hooks on a
        container CONTAINING Embody are skipped in copy mode.
        run_hooks=False skips hooks and ships them as-is (self-updater
        backup).

        Args:
            target: COMP to export (default: the Embody COMP).
            save_path: Output .tox path (default release/{name}-v{ver}.tox).
            run_hooks: Run hook DATs (auto-suppressed for nested exports).
            hook_mode: 'copy' (default) | 'live'.

        Returns:
            True when saved and no hook failed. (post_release failing
            after a good save returns False with the .tox on disk.)
        """
        if target is None:
            target = self.my
        if save_path is None:
            version = self.my.par.Version.eval()
            save_path = str(
                Path(project.folder).parents[0] / 'release'
                / f"{target.name}-v{version}.tox"
            )
        if hook_mode not in ('copy', 'live'):
            self.Log(
                f"Unknown hook_mode '{hook_mode}' -- export aborted "
                f"(use 'copy' or 'live')", "ERROR")
            return False

        # The latch is COMP storage so the stale exporting instance and any
        # fresh instance created by a mid-export extension reinit read the
        # SAME latch (stripping our own source DATs can trigger a reinit).
        latch_active = bool(
            self.my.fetch('_release_hook_active', False, search=False))

        # Never copy-stage the live Embody COMP -- neither itself nor any
        # ancestor container whose copy would drag a live Envoy into
        # /sys/quiet (second-Envoy init, port grab, registry pollution).
        target_prefix = (target.path if target.path.endswith('/')
                         else target.path + '/')
        stages_live_embody = (target == self.my
                              or self.my.path.startswith(target_prefix))

        # Copy-mode engagement: hooks wanted, at least one VALID hook
        # present, staging cannot capture the live Embody COMP, and we
        # are not already inside a hook. Embody-self exports never probe
        # here (the live path below owns them) -- probing twice would
        # double-warn about imposter DATs on every project.save.
        if (run_hooks and hook_mode == 'copy' and not latch_active
                and target != self.my):
            has_pre = self._findReleaseHook(target, 'pre_release') is not None
            has_post = self._findReleaseHook(target, 'post_release') is not None
            if has_pre or has_post:
                if not stages_live_embody:
                    return self._exportPortableViaCopy(
                        target, str(save_path), has_pre, has_post)
                # Ancestor of the live Embody COMP: copy staging is
                # forbidden, and running the hooks live would betray
                # copy-mode expectations -- skip them, loudly.
                self.Log(
                    f"Release hooks on {target.path} SKIPPED: the "
                    f"target contains the live Embody COMP and cannot "
                    f"be copy-staged. Pass hook_mode='live' to run "
                    f"hooks in place.", "WARNING")

        # Live path. Hooks run here only in explicit 'live' mode or when
        # the target IS the Embody COMP (self-release machinery path).
        # Phase 0: Author's pre_release hook. Runs BEFORE collection so the
        # export captures the post-hook state; save_path is already
        # resolved -- hooks always see the final path. A failure aborts: a
        # half-prepared component must not ship.
        hooks_enabled = (run_hooks and not latch_active
                         and (hook_mode == 'live' or target == self.my))
        if hooks_enabled:
            hook_found, hook_ok = self._runReleaseHook(
                target, 'pre_release', (str(save_path),))
            if not hook_ok:
                self.Log(
                    f"Portable .tox export aborted: pre_release hook "
                    f"failed on {target.path}", "ERROR")
                return False

        # Phases 1-3 are exception-contained: any unexpected raise (a DAT
        # family with a 'file' par but no 'syncfile' like File In, a hook
        # that destroyed the target, ...) degrades to success=False so the
        # restore below and the post_release hook ALWAYS get their turn.
        saved_state = []
        saved_tags = []  # list of (op_ref, set_of_removed_tags, path)
        transient_snapshot = []  # [(par, value)] -- runtime-status scrub (A-50)
        success = False
        try:
            # Phase 1: Collect file references and externalization params to
            # strip. Include the target itself -- its externaltox/
            # enableexternaltox would be baked into the .tox and confuse
            # recipients.
            for op_ref in [target] + target.findChildren():
                if op_ref.family == 'DAT' and hasattr(op_ref.par, 'file'):
                    file_val = op_ref.par.file.eval()
                    # Not every DAT with 'file' has 'syncfile' (File In
                    # DAT doesn't) -- collect defensively.
                    sync_par = getattr(op_ref.par, 'syncfile', None)
                    sync_val = (bool(sync_par.eval())
                                if sync_par is not None else False)
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
                            'path': op_ref.path,
                            'family': 'DAT',
                            'file': file_val,
                            'file_readonly': op_ref.par.file.readOnly,
                            'syncfile': sync_val,
                            'has_syncfile': sync_par is not None,
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
                            'path': op_ref.path,
                            'family': 'COMP',
                            'externaltox': tox_val,
                            'externaltox_readonly': op_ref.par.externaltox.readOnly,
                            'enableexternaltox': enable_val,
                        })

            # Phase 1b: Collect Embody tags to strip from all descendants
            # (including the target itself). Recipients don't need Embody
            # metadata -- it would cause confusion if they have Embody
            # installed.
            embody_tags = set(self.getTags())

            # Check target itself, then all descendants. Paths captured at
            # collection time so exception handlers never have to format a
            # possibly-destroyed op.
            for op_ref in [target] + target.findChildren():
                found = set(op_ref.tags) & embody_tags
                if found:
                    saved_tags.append((op_ref, found, op_ref.path))

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
                        if entry['has_syncfile']:
                            op_ref.par.syncfile = False
                    elif entry['family'] == 'COMP':
                        op_ref.par.externaltox.readOnly = False
                        op_ref.par.externaltox = ''
                        op_ref.par.enableexternaltox = False
                except Exception as e:
                    self.Log(
                        f"Failed to strip {entry['path']}: {e}", "WARNING")

            # Strip Embody tags.
            for op_ref, tags_to_remove, op_path in saved_tags:
                try:
                    for tag in tags_to_remove:
                        op_ref.tags.remove(tag)
                except Exception as e:
                    self.Log(
                        f"Failed to strip tags from {op_path}: {e}",
                        "WARNING")

            # Phase 2c: Reset runtime-status pars (A-50) -- 'Testing',
            # 'Saved <time>', 'Running on port N' must never ship in a
            # released artifact. Snapshot taken; Phase 4 always restores.
            transient_snapshot = self._scrubTransientPars(target)

            # Phase 2d: Empty the log buffer DATs (A-50, same rule). The FIFO
            # the Log() ring writes to is a RUNTIME DISPLAY buffer, but its
            # rows are saved with the component -- so a released .tox shipped
            # whatever this developer's session happened to log, including
            # host/convoy identifiers and local paths (found 2026-08-03 by
            # expanding the release .tox). The file log and the ring buffer
            # remain the real record; Phase 4 puts the rows back.
            log_snapshot = self._scrubLogBuffers(target)

            # Issue #86: this save writes the whole subtree, so Embot's
            # annotation parts would ship in the portable .tox.
            self._retireVizBeforeWrite(target.path)

            # Phase 3: Save the .tox.
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

        # Phase 4: Restore all references (always -- after a failed save or
        # a mid-flight error too; re-setting a value that was never stripped
        # is a harmless no-op).
        # Safe ONLY because setting externaltox/enableexternaltox does not
        # trigger a mid-session load on TD 2025 -- live content survives.
        # If TD ever made a par-set reload, this restore would wipe it.
        for entry in saved_state:
            try:
                op_ref = entry['op']
                if entry['family'] == 'DAT':
                    op_ref.par.file = entry['file']
                    op_ref.par.file.readOnly = entry['file_readonly']
                    if entry['has_syncfile']:
                        op_ref.par.syncfile = entry['syncfile']
                elif entry['family'] == 'COMP':
                    op_ref.par.externaltox = entry['externaltox']
                    op_ref.par.externaltox.readOnly = entry['externaltox_readonly']
                    op_ref.par.enableexternaltox = entry['enableexternaltox']
            except Exception as e:
                self.Log(
                    f"Failed to restore {entry['path']}: {e}", "WARNING")

        # Restore Embody tags (always, even on save failure).
        for op_ref, tags_to_restore, op_path in saved_tags:
            try:
                for tag in tags_to_restore:
                    op_ref.tags.add(tag)
            except Exception as e:
                self.Log(
                    f"Failed to restore tags on {op_path}: {e}", "WARNING")

        # Restore runtime-status readouts (always -- live-mode exports run
        # on the session's real comp and must hand its status back).
        self._restoreTransientPars(transient_snapshot)
        self._restoreLogBuffers(log_snapshot)

        # Phase 5: Author's post_release hook -- the reset half of the
        # set/reset contract. Runs whenever pre_release did not abort,
        # EVEN IF the save failed, so a reset can always rely on
        # executing. Receives (save_path, success) as run args.
        if hooks_enabled:
            hook_found, hook_ok = self._runReleaseHook(
                target, 'post_release', (str(save_path), success))
            if hook_found and not hook_ok:
                if success:
                    self.Log(
                        f"post_release hook failed AFTER a successful "
                        f"export -- the .tox exists at {save_path}",
                        "ERROR")
                success = False

        return success

    def _runReleaseHook(self, target: 'OP', hook_name: str,
                        run_args: tuple) -> tuple:
        """Run a release hook DAT if the target has one.

        Looks for a TEXT DAT named `hook_name` as a DIRECT child of
        `target` only -- nested hooks are deliberately ignored so a
        third-party component embedded inside the target can never
        inject a hook into someone else's export, and non-text DATs
        (tables, selects) that merely share the name are never executed.
        The DAT executes synchronously via DAT.run() with `run_args`
        exposed to the script as the `args` tuple; inside the script,
        `me` is the hook DAT and parent() is the COMP the hook belongs
        to -- the staged copy for a copy-mode pre_release, the live
        target otherwise.

        The '_release_hook_active' COMP-storage latch is held while the
        hook executes, so a nested ExportPortableTox call made from
        inside a hook runs with hooks suppressed (no infinite recursion;
        exporting a sub-component from a pre_release script is a
        supported pattern). Storage -- not an instance attribute --
        because the export's own strip phase can reinit this extension
        mid-call when the Embody COMP is the target, and a nested hook
        call resolving the FRESH instance must read the same latch the
        stale exporting instance set. (A reinit fired while a hook is
        actually running clears the latch -- __init__ hygiene wins over
        that exotic window.)

        Returns:
            (found, ok): found is True when a hook DAT exists; ok is
            False only when the hook ran and raised.
        """
        if not target.valid:
            self.Log(
                f"{hook_name} skipped -- hook owner no longer exists "
                f"(destroyed by an earlier hook?)", "WARNING")
            return (False, True)
        hook = self._findReleaseHook(target, hook_name)
        if hook is None:
            return (False, True)
        # Capture the path NOW: if the hook destroys itself while running,
        # formatting hook.path inside the except handler would raise a
        # secondary tdError that escapes to the caller.
        hook_path = hook.path
        self.Log(f"Running {hook_name} hook: {hook_path}", "INFO")
        self.my.store('_release_hook_active', True)
        try:
            hook.run(*run_args)
            return (True, True)
        except Exception as e:
            self.Log(
                f"{hook_name} hook raised: {hook_path}: {e}", "ERROR")
            return (True, False)
        finally:
            self.my.store('_release_hook_active', False)

    def ReleaseAll(self, root: 'OP' = None,
                   out_dir: Optional[str] = None) -> dict:
        """Export every releasable COMP as its own portable .tox.

        A component opts in by being BOTH Embody-tracked (it carries an
        externalization tag -- it is yours) AND carrying a valid release
        hook -- a Text DAT named 'pre_release' or 'post_release' as a
        DIRECT child (the exact convention ExportPortableTox executes).
        The tracked requirement exists because third-party components
        arrive with their authors' hook DATs baked in (Private
        Investigator-style tools ship them; found in the wild:
        AlphaMoonbase's tweener) -- a
        hooks-only scan would execute foreign release machinery. Export
        an untracked component explicitly via ExportPortableTox instead.
        Each target goes through the normal single-component export
        (copy staging, its own hooks, artifact hygiene included).

        Nested hook-bearing components release independently -- each gets
        its own standalone .tox in addition to shipping as content inside
        any ancestor's artifact. Do not call from inside a release hook:
        the re-entrancy latch would suppress every target's hooks.

        Args:
            root: Scan scope. None scans the whole project, excluding TD
                  system networks (/sys, /local) and the Embody COMP
                  itself; pass a COMP to scan only it and its descendants.
            out_dir: Directory for the .tox files (created if missing).
                     Defaults to the 'release' folder beside the project
                     folder. Each component saves as {out_dir}/{name}.tox;
                     duplicate names get a numeric suffix and a WARNING.

        Returns:
            {'targets': [comp paths], 'released': [tox paths],
             'failed': [comp paths]}. A per-component failure is logged
            loudly and does not halt the batch.
        """
        if out_dir is None:
            out_dir = str(Path(project.folder).parents[0] / 'release')
        os.makedirs(out_dir, exist_ok=True)

        targets = self._findReleaseTargets(root)

        result = {'targets': [c.path for c in targets],
                  'released': [], 'failed': []}
        if not targets:
            self.Log(
                'ReleaseAll: no tracked components carry release hooks -- '
                'nothing to export. Externalize a component and add a '
                'pre_release/post_release Text DAT to opt it in.',
                'WARNING')
            return result

        self.Log(f'ReleaseAll: exporting {len(targets)} component(s) to '
                 f'{out_dir}', 'INFO')
        used_names = {}
        for c in targets:
            target_path = c.path
            n = used_names.get(c.name, 0)
            used_names[c.name] = n + 1
            fname = f'{c.name}.tox' if n == 0 else f'{c.name}_{n + 1}.tox'
            if n:
                self.Log(
                    f'ReleaseAll: duplicate component name "{c.name}" -- '
                    f'saving {target_path} as {fname}', 'WARNING')
            save_path = str(Path(out_dir) / fname)
            try:
                ok = self.ExportPortableTox(target=c, save_path=save_path)
            except Exception as e:
                ok = False
                self.Log(f'ReleaseAll: unexpected error exporting '
                         f'{target_path}: {e}', 'ERROR')
            if ok:
                result['released'].append(save_path)
            else:
                result['failed'].append(target_path)

        level = 'SUCCESS' if not result['failed'] else 'WARNING'
        failed_note = (f" ({', '.join(result['failed'])})"
                       if result['failed'] else '')
        self.Log(
            f"ReleaseAll complete: {len(result['released'])} released, "
            f"{len(result['failed'])} failed{failed_note}", level)
        return result

    def _findReleaseTargets(self, root: 'OP' = None) -> list:
        """Discovery half of ReleaseAll: tracked AND hook-bearing COMPs.

        Pure scan -- no exports, no hook execution -- so tests can pin
        the targeting rules without touching live components. root=None
        scans the whole project minus TD system networks (/sys, /local
        -- staged copies in /sys/quiet carry hook DATs and must never be
        re-released) and the Embody COMP itself. The tag check runs
        FIRST so untracked comps never even get the imposter-hook
        warning from _findReleaseHook.
        """
        if root is None:
            def _excluded(c):
                p = c.path
                return (p.startswith('/sys/') or p.startswith('/local/')
                        or p == self.my.path
                        or p.startswith(self.my.path + '/'))
            candidates = [c for c in op('/').findChildren(type=COMP)
                          if not _excluded(c)]
        else:
            candidates = [root] + root.findChildren(type=COMP)

        embody_tags = set(self.getTags())
        targets = [
            c for c in candidates
            if (set(c.tags) & embody_tags)
            and (self._findReleaseHook(c, 'pre_release') is not None
                 or self._findReleaseHook(c, 'post_release') is not None)]
        targets.sort(key=lambda c: c.path)
        return targets

    def _findReleaseHook(self, target: 'OP', hook_name: str):
        """Return the valid release hook for `target`, or None.

        A valid hook is a TEXT DAT named `hook_name` that is a DIRECT
        child of `target`. Non-text DATs (tables, selects) and COMPs
        that merely share the name are warned about and never executed
        -- DAT.run() on a table would execute its cells as Python.
        """
        if not target.valid:
            return None
        hook = target.op(hook_name)
        if hook is None:
            return None
        if not hook.isDAT or hook.type != 'text':
            self.Log(
                f"{hook_name} on {target.path} is not a Text DAT -- "
                f"ignored", "WARNING")
            return None
        return hook

    def _exportPortableViaCopy(self, target: 'OP', save_path: str,
                               has_pre: bool, has_post: bool) -> bool:
        """Portable export with Private Investigator-style copy staging
        (the default hook mode).

        The target is copied into the cooking-disabled /sys/quiet staging
        area; the copy is immediately neutralized -- Embody tags removed
        AND file-sync bindings disabled (syncfile/enableexternaltox off),
        so a hook edit can never write through to real source files and
        no sweep can mistake staged ops for tracked ones; pre_release
        runs ON THE COPY; the captured hook DATs are destroyed from the
        copy (rename-proof); the copy is exported through the normal
        strip/save pipeline (run_hooks=False) and destroyed. post_release
        then runs on the ORIGINAL with (save_path, success) whenever
        pre_release completed -- including when it completed but
        destroyed the staged copy (success False). The live target is
        never mutated by any of this.

        A pre_release RAISE keeps the staged copy under /sys/quiet
        (renamed <name>_release_failed) so the author can inspect exactly
        what the hook did -- in the same session; /sys is not saved with
        the project. post_release does not run after a raise.
        """
        quiet = op('/sys/quiet')
        if quiet is None:
            self.Log(
                "/sys/quiet staging area not found -- falling back to "
                "live hook mode for this export", "WARNING")
            return self.ExportPortableTox(
                target=target, save_path=save_path, run_hooks=True,
                hook_mode='live')

        # Issue #86: retire Embot BEFORE the snapshot. This branch never reaches
        # the guard in ExportPortableTox's live path in time -- the copy below
        # freezes whatever is standing in the live tree, and the recursive export
        # then guards the CANDIDATE's path under /sys/quiet, which no live bot is
        # ever inside. So without this, a release export run right after an agent
        # build inside `target` (the case the relocation gate makes more likely,
        # since queued-batch evidence deliberately commits him into a COMP where
        # a batch is happening) ships nine envoy_bot_* annotateCOMPs in the
        # released artifact.
        self._retireVizBeforeWrite(target.path)

        try:
            candidate = quiet.copy(target)
        except Exception as e:
            self.Log(
                f"Release staging copy failed: {e} -- export aborted "
                f"(live component untouched)", "ERROR")
            return False
        cand_path = candidate.path

        success = False
        keep_candidate = False
        candidate_gone = False
        try:
            # Neutralize: the copy carries the original's Embody tags AND
            # live file bindings -- file/syncfile paths resolve against
            # the project folder regardless of op location, so the staged
            # copy's synced DATs point at the REAL source files. Kill
            # both BEFORE any hook runs: a hook edit must never write
            # through to a live source file, no tag-driven sweep may
            # mistake staged ops for tracked ones, and a kept-failed
            # candidate must be inert. Artifact content is unaffected --
            # the export core strips the same bindings anyway.
            neutralize_failed = False
            try:
                embody_tags = set(self.getTags())
                candidate_ops = [candidate] + candidate.findChildren()
            except Exception as e:
                self.Log(
                    f"Failed to enumerate staged copy: {e}", "ERROR")
                neutralize_failed = True
                candidate_ops = []
            for op_ref in candidate_ops:
                try:
                    for tag in set(op_ref.tags) & embody_tags:
                        op_ref.tags.remove(tag)
                    if op_ref.family == 'DAT':
                        sync_par = getattr(op_ref.par, 'syncfile', None)
                        if sync_par is not None and sync_par.eval():
                            op_ref.par.syncfile = False
                    elif (op_ref.family == 'COMP'
                          and hasattr(op_ref.par, 'enableexternaltox')
                          and op_ref.par.enableexternaltox.eval()):
                        op_ref.par.enableexternaltox = False
                except Exception as e:
                    neutralize_failed = True
                    self.Log(
                        f"Failed to neutralize staged op: {e}", "WARNING")
            if neutralize_failed:
                # Never run hooks on a partially-neutralized copy: one
                # still-synced DAT could write through to a real source
                # file. Fail loud; the finally block destroys the copy.
                self.Log(
                    "Portable .tox export aborted: staged copy could not "
                    "be fully neutralized.", "ERROR")
                return False

            # No transient-par scrub here: the export core (the live path
            # this method delegates to for the candidate) scrubs registered
            # pars right before the save, in EVERY mode -- the registry is
            # the last word, and a pre_release hook cannot ship a session
            # value for a registered par (see _TRANSIENT_STATUS_PARS).

            # Capture the copy's hook DATs NOW -- a pre hook that renames
            # itself must not let hook code escape into the artifact.
            cand_hooks = []
            for hook_name in ('pre_release', 'post_release'):
                hook = candidate.op(hook_name)
                if hook is not None and hook.isDAT and hook.type == 'text':
                    cand_hooks.append(hook)

            # pre_release runs on the COPY -- me is the copy's hook DAT,
            # parent() is the staged candidate.
            if has_pre:
                hook_found, hook_ok = self._runReleaseHook(
                    candidate, 'pre_release', (str(save_path),))
                if not hook_ok:
                    if candidate.valid:
                        keep_candidate = True
                        try:
                            candidate.name = target.name + '_release_failed'
                            cand_path = candidate.path
                        except Exception:
                            pass
                        # Fully inert while parked: blank global OP
                        # shortcuts on the kept tree so it can never
                        # contend with the live original for op.X.
                        try:
                            for o in ([candidate]
                                      + candidate.findChildren(type=COMP)):
                                if o.par.opshortcut.eval():
                                    o.par.opshortcut = ''
                        except Exception:
                            pass
                        self.Log(
                            f"Portable .tox export aborted: pre_release "
                            f"hook failed. The staged copy was kept for "
                            f"inspection at {cand_path} -- inspect it in "
                            f"this session (staging is not saved with the "
                            f"project) and delete it when done.", "ERROR")
                    else:
                        self.Log(
                            "Portable .tox export aborted: pre_release "
                            "hook failed and destroyed the staged copy.",
                            "ERROR")
                    return False
                if not candidate.valid:
                    # Pre COMPLETED (no raise) but destroyed the staged
                    # copy -- nothing to export, yet the post_release
                    # guarantee ('runs once pre completes') still holds:
                    # fall through with success False.
                    candidate_gone = True
                    self.Log(
                        "Portable .tox export failed: pre_release "
                        "destroyed the staged copy -- nothing to export.",
                        "ERROR")

            if not candidate_gone:
                # Hook code never ships: destroy the captured hook DATs
                # (rename-proof) AND any hook-named text DAT the pre hook
                # itself created on the copy after capture.
                for hook in cand_hooks:
                    if hook.valid:
                        hook.destroy()
                for hook_name in ('pre_release', 'post_release'):
                    hook = candidate.op(hook_name)
                    if (hook is not None and hook.isDAT
                            and hook.type == 'text'):
                        hook.destroy()

                # Export the candidate through the normal pipeline.
                success = self.ExportPortableTox(
                    target=candidate, save_path=save_path, run_hooks=False)
        finally:
            if not keep_candidate and candidate.valid:
                try:
                    candidate.destroy()
                except Exception as e:
                    self.Log(
                        f"Failed to destroy staged copy {cand_path}: {e}",
                        "WARNING")

        # post_release runs on the ORIGINAL -- even when the save failed.
        if has_post:
            hook_found, hook_ok = self._runReleaseHook(
                target, 'post_release', (str(save_path), success))
            if hook_found and not hook_ok:
                if success:
                    self.Log(
                        f"post_release hook failed AFTER a successful "
                        f"export -- the .tox exists at {save_path}",
                        "ERROR")
                success = False
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
        Registered transient status pars and TDN value-omit pars are likewise
        excluded (constant mode only -- an expression edit must still dirty)
        for comps where they are registered: the export no longer serializes
        their session values, so a status flip must not mark the COMP dirty
        and trigger a byte-identical main-thread re-export.
        """
        skip = {'Build', 'Date', 'Touchbuild'}
        shortcut = EmbodyExt._registryShortcut(operator)
        if shortcut:
            skip |= set(EmbodyExt._TRANSIENT_STATUS_PARS.get(shortcut, ()))
            skip |= set(EmbodyExt._TDN_VALUE_OMIT_PARS.get(shortcut, ()))
        out = []
        for p in operator.pars():
            try:
                if (p.name in skip and p.mode.name == 'CONSTANT') \
                        or p.isDefault:
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

    @property
    def _tdn_fingerprints(self) -> dict:
        """TDN fingerprint baselines, kept in ownerComp storage so they
        SURVIVE extension reinit. As an instance attribute, every source
        edit re-initialized the cache and the next sweep's assume-clean
        seeding re-baselined unsaved changes as clean -- silently wiping
        real dirty state from the manager (observed 2026-07-20: 13 dirty
        COMPs vanished after an EmbodyExt.py edit). Mutated in place, never
        re-store()d (mirrors expanded_paths); excluded from TDN export via
        SKIP_STORAGE_KEYS; cleared at project open by ReconstructTDNComps
        so a .toe-persisted copy cannot poison fresh baselines."""
        cache = self.my.fetch('_tdn_fingerprints', None, search=False)
        if cache is None:
            cache = {}
            self.my.store('_tdn_fingerprints', cache)
        return cache

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
            if self._cellVal(i, 'path', table=table) == op_path:
                if has_strategy_col and self._cellVal(
                        i, 'strategy', table=table) == strategy:
                    return self._cellVal(i, 'rel_file_path', table=table)
                elif not has_strategy_col:
                    return self._cellVal(i, 'rel_file_path', table=table)
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
            if self._cellVal(i, 'strategy', table=table) != 'tdn':
                continue
            path = self._cellVal(i, 'path', table=table)
            if path == exclude_path:
                continue
            rel = self._cellVal(i, 'rel_file_path', table=table)
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
            if self._cellVal(i, 'path', table=table) == comp.path:
                s = self._cellVal(i, 'strategy', table=table)
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
        """Find a COMP in the externalizations table and return (path, strategy).

        Keyed lookup by the 'path' column (the table's first column) via TD's
        native row-name index -- O(1), not an O(rows) Python scan. This is a hot
        path: the autosave recorder calls it once per ancestor level on EVERY
        mutating MCP op (and per batch sub-op), so a linear scan here was a real
        per-op regression on large builds."""
        table = self.Externalizations
        if table[0, 'strategy'] is None:
            # Legacy table without a strategy column -- any matching row is tox.
            return (comp_path, 'tox') if table[comp_path, 'path'] is not None else None
        cell = table[comp_path, 'strategy']
        if cell is None:
            return None
        s = cell.val
        return (comp_path, s) if s in ('tox', 'tdn') else None

    def _saveByStrategy(self, op_path: str, strategy: str) -> None:
        """Save a COMP using the appropriate strategy."""
        if strategy == 'tdn':
            self.SaveTDN(op_path)
        else:
            self.Save(op_path)

    # ==========================================================================
    # GIT STATUS (uncommitted detection for the manager UI)
    # ==========================================================================
    # A SECOND status axis, distinct from "unsaved" (live-vs-disk). Externalized
    # DAT scripts use TD's bidirectional syncfile, so they are always in sync with
    # disk -- their only meaningful "changed" state is git-relative (on disk but
    # not committed). Computed once per refresh sweep and stored at runtime (never
    # written to externalizations.tsv, which would churn). Powers the orange badge
    # for TOX/TDN/DAT alike. Self-disables outside a git repo.

    def _findGitRootSync(self):
        """Walk up from project.folder for a .git dir; Path or 'no-git' -- see embody_git."""
        return mod.embody_git.find_git_root_sync(self)

    @staticmethod
    def _parseGitPorcelain(output: str) -> dict:
        """Parse `git status --porcelain -z` into {repo_rel_posix: code} -- see embody_git."""
        return mod.embody_git.parse_git_porcelain(output)

    @staticmethod
    def _mapChangedToOps(changed, project_prefix, rows):
        """Map a git {repo_rel_posix: code} set to {op_path: code} -- see embody_git."""
        return mod.embody_git.map_changed_to_ops(changed, project_prefix, rows)

    @staticmethod
    def _rowIsUnsaved(dirty_val) -> bool:
        """Whether a manager row has unsaved in-TD changes -- see embody_git."""
        return mod.embody_git.row_is_unsaved(dirty_val)

    @staticmethod
    def _rowHasChanges(dirty_val, uncommitted) -> bool:
        """Whether a manager row has pending changes on either axis -- see embody_git."""
        return mod.embody_git.row_has_changes(dirty_val, uncommitted)

    def _updateGitStatus(self) -> None:
        """Kick off an ASYNC git-uncommitted scan on a worker thread -- see embody_git."""
        return mod.embody_git.update_git_status(self)

    # Dirty state is RUNTIME-ONLY (field 2026-08-20: persisting it made
    # every refresh sweep churn the committed tsv, so one save read as a
    # multi-file git event). The tsv keeps a blank 'dirty' column for
    # schema compatibility; _migrateTableSchema blanks legacy values once.
    # Keys are op paths; stale keys after a delete/rename are harmless --
    # readers walk table rows, and the next sweep re-derives.
    def _setDirtyState(self, path: str, value) -> None:
        states = self._dirtyStates()
        text = ('' if value in (None, False, '', 'False')
                else ('True' if value is True else str(value)))
        if text:
            states[str(path)] = text
        else:
            states.pop(str(path), None)
        # Worker-readable mirror {op_path: repo-relative file} in a sys
        # slot (reload-proof, like sys._embody_pyenv_*): EnvoyExt's
        # preflight runs on the worker thread and may not touch TD
        # objects, so the repo-relative path is resolved HERE, on the
        # main thread. Total -- a mirror failure must never break a sweep.
        try:
            mirror = getattr(sys, '_embody_dirty_files', None)
            if mirror is None:
                mirror = sys._embody_dirty_files = {}
            if text:
                rel = self.normalizePath(
                    self._cellVal(str(path), 'rel_file_path') or '')
                if rel:
                    prefix = os.path.relpath(
                        project.folder,
                        str(self._findProjectRoot())).replace('\\', '/')
                    mirror[str(path)] = (
                        rel if prefix == '.' else prefix + '/' + rel)
            else:
                mirror.pop(str(path), None)
        except Exception:
            pass

    def _dirtyStates(self) -> dict:
        """The dirty-state dict, ownerComp-storage-backed like
        _tdn_fingerprints: an instance dict is wiped by every extension
        reinit, and the next sweep would adopt unsaved 'Par' changes as
        clean (the 2026-07-20 fingerprint incident class)."""
        states = self.my.fetch('_dirty_states', None, search=False)
        if states is None:
            states = {}
            self.my.store('_dirty_states', states)
        return states

    def DirtyState(self, path: str) -> str:
        """Runtime dirty flag for a tracked op: '' | 'True' | 'Par'."""
        return self._dirtyStates().get(str(path), '')

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
                if dirty or self.DirtyState(oper.path) != 'Par':
                    self._setDirtyState(oper.path, dirty)
            except Exception as e:
                self.Log(f"Failed to update dirty state for {oper.path}: {e}", "DEBUG")
            if dirty and update:
                # Only a save that actually WROTE counts toward the
                # 'Saved N externalizations' tally -- a guard refusal
                # reported as a save is a contradictory signal (review).
                if self.Save(oper.path):
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
                self._setDirtyState(oper.path, 'True' if dirty else '')
                if dirty and update:
                    if self.SaveTDN(oper.path):
                        updates.append(oper.path)

        return updates

    # Per-frame time budget (ms) for one chunk of the passive TDN dirty sweep.
    # A 60fps frame is 16.6ms; 8ms leaves room for the rest of the frame.
    _DIRTY_SWEEP_BUDGET_MS = 8.0

    def _dirtyHandlerDeferred(self) -> None:
        """Passive dirty scan, spread across frames so it never blocks one.

        Same observable result as dirtyHandler(False) -- every TOX COMP's
        dirty flag and every TDN COMP's fingerprint-derived flag land in the
        table -- but the TDN fingerprint pass is chunked under a per-frame
        time budget instead of fingerprinting every COMP in a single frame.
        That pass measured 255ms on a 66-TDN-COMP project: a ~15-frame stall
        at 60fps, paid on every Refresh, so on every save.

        It is NOT threadable: it reads DATs, operators and parameters, all of
        which are main-thread-only (rules/td-python.md, where a read counts as
        a write). Chunking with run(delayFrames=) is the sanctioned rung for
        main-thread-bound work.

        dirtyHandler(True) -- the Update()/save path -- stays synchronous: its
        callers need the saves to have actually happened when it returns.
        """
        # The TOX pass is one cheap flag read per COMP -- keep it inline.
        for oper in self.getExternalizedOps(COMP, strategy='tox'):
            dirty = oper.dirty
            try:
                # Preserve 'Par' dirty state when oper.dirty is False -- see
                # dirtyHandler; parameter changes clear only on Save.
                if dirty or self.DirtyState(oper.path) != 'Par':
                    self._setDirtyState(oper.path, dirty)
            except Exception as e:
                self.Log(f"Failed to update dirty state for {oper.path}: {e}",
                         "DEBUG")

        # Bump the generation BEFORE the enabled check: an early return here
        # used to leave an in-flight chain matching, so switching TDN off
        # mid-sweep kept it fingerprinting and writing TDN dirty cells.
        gen = getattr(self, '_dirty_gen', 0) + 1
        self._dirty_gen = gen
        if not self._tdnEnabled():
            self._dirty_queue = []
            return

        exclude_tag = self.my.par.Tdnexcludetag.eval()
        # Skip root "/" (Full Project export) and app-managed excluded COMPs,
        # exactly as dirtyHandler does.
        queue = [
            oper.path for oper in self.getExternalizedOps(COMP, strategy='tdn')
            if oper.path != '/' and exclude_tag not in oper.tags]
        # RESUME an identical queue instead of restarting it. Refresh is
        # pulse-driven and the manager calls it synchronously on every tree
        # click, so resetting the index each time starved the sweep: a user
        # clicking faster than the sweep drains meant it never finished and
        # every click paid a fresh partial pass.
        if queue != getattr(self, '_dirty_queue', None):
            self._dirty_idx = 0
        self._dirty_queue = queue
        # Resolve the per-sweep constants ONCE and carry them with the queue.
        # _getTDNPaths() is a full table scan; reading it per chunk put ~33 of
        # them where the synchronous sweep had 1 -- and it sat outside the
        # frame budget, so each chunk really cost scan + budget.
        self._dirty_tdn_paths = self._getTDNPaths()
        self._dirty_exclude_tag = exclude_tag
        # Defer even the FIRST chunk, so the frame that triggered the Refresh
        # (the user's save) does no fingerprinting at all.
        run(f"op('{self.my}').ext.Embody._sweepTDNDirtyChunk({gen})",
            delayFrames=1, fromOP=self.my)

    def _sweepTDNDirtyChunk(self, gen: int) -> None:
        """One frame's worth of the passive TDN dirty sweep; re-arms until done.

        Bails immediately when superseded by a newer sweep -- a generation
        mismatch, which also covers an extension reinit having dropped the
        queue -- or when perform mode is on.
        """
        import time
        if gen != getattr(self, '_dirty_gen', None) or self._performMode:
            return
        queue = getattr(self, '_dirty_queue', None)
        if not queue:
            return
        # Resolved once per sweep by _dirtyHandlerDeferred, not per chunk.
        tdn_paths = getattr(self, '_dirty_tdn_paths', None)
        if tdn_paths is None:
            tdn_paths = self._getTDNPaths()
        exclude_tag = getattr(self, '_dirty_exclude_tag', None)
        if exclude_tag is None:
            exclude_tag = self.my.par.Tdnexcludetag.eval()
        deadline = time.perf_counter() + self._DIRTY_SWEEP_BUDGET_MS / 1000.0
        i = getattr(self, '_dirty_idx', 0)
        while i < len(queue):
            oper = op(queue[i])
            i += 1
            if oper is None:  # deleted since the queue was built
                continue
            # The FINGERPRINT is inside the guard too. Left outside it, one
            # COMP whose fingerprint raised escaped this run() callback, so the
            # chain never re-armed -- and because every later Refresh rebuilds
            # the same queue and stops at the same COMP, TDN dirty badges died
            # for the rest of the session with nothing pointing at the cause.
            # A detached callback must not be able to fail silently.
            try:
                dirty = self._isTDNDirty(oper, tdn_paths, exclude_tag)
                self._setDirtyState(oper.path, 'True' if dirty else '')
            except Exception as e:
                self.Log(f"Dirty scan failed for {oper.path}: {e}", "WARNING")
            # Always finish the COMP in hand: a single fingerprint is not
            # divisible, so the budget is checked AFTER the work, not before.
            if time.perf_counter() >= deadline:
                break
        self._dirty_idx = i
        if i < len(queue):
            run(f"op('{self.my}').ext.Embody._sweepTDNDirtyChunk({gen})",
                delayFrames=1)
            return
        self._dirty_queue = []
        # Repaint the manager so the badges reflect the finished sweep.
        try:
            self.my.op('list/inject_parents').cook(force=True)
            self.lister.reset()
        except Exception:
            pass

    def updateDirtyStates(self, externalizationsFolder: str) -> None:
        """Update dirty states and check for path/parameter changes."""
        # Passive scan, chunked across frames. It never saves, so it never
        # returns updates -- the "unsaved tox" tally below has always come
        # from param_changes here; dirtyHandler(True) on Update() does saves.
        self._dirtyHandlerDeferred()
        # Second status axis: git-uncommitted files (orange badge). Read-only,
        # folder-scoped, self-disabling outside a repo -- see _updateGitStatus.
        self._updateGitStatus()
        param_changes = []

        # Strategy per path, resolved in ONE table pass. _getCompStrategy()
        # scans the whole table per COMP, so calling it inside this loop was
        # O(comps x rows) -- ~20k cell reads / ~83ms of blocked main thread on
        # a 300-row project, every Refresh. Same rows, same answer, one pass.
        table = self.Externalizations
        has_strategy_col = (table is not None
                            and table[0, 'strategy'] is not None)
        strategy_by_path = {}
        if table is not None:
            for i in range(1, table.numRows):
                path = self._cellVal(i, 'path', table=table)
                if not path or path in strategy_by_path:
                    continue
                if not has_strategy_col:
                    strategy_by_path[path] = 'tox'  # legacy pre-strategy table
                else:
                    s = self._cellVal(i, 'strategy', table=table)
                    if s in ('tox', 'tdn'):
                        strategy_by_path[path] = s

        for oper in self.getExternalizedOps(COMP) + self.getExternalizedOps(DAT):
            # TDN-strategy COMPs don't use externaltox -- their rel_file_path
            # is managed by _handleTDNAddition / _addToTable, not the par.
            # Their dirty state (structural AND parameter) was already fully
            # evaluated by the dirty scan above via the network fingerprint,
            # so there is no separate compareParameters() pass here. Skip them
            # to avoid overwriting the .tdn path with "".
            if oper.family == 'COMP' and strategy_by_path.get(oper.path) == 'tdn':
                continue

            # A tracked DAT path can resolve to a non-file-backed DAT
            # (selectDAT, mergeDAT, ...) after a delete/rename swap
            # (issue #54). Leave the row for checkOpsForContinuity to
            # reconcile as "replaced" -- syncing here would blank
            # rel_file_path and erase the recovery pointer.
            if oper.family == 'DAT' and not hasattr(oper.par, 'file'):
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
                self._setDirtyState(oper.path, 'Par')

        if param_changes:
            plural = 's' if len(param_changes) > 1 else ''
            self.Log(f"Found {len(param_changes)} COMP{plural} with param "
                     f"changes", "INFO")

    # ==========================================================================
    # ADDITION / SUBTRACTION HANDLING
    # ==========================================================================

    def handleAddition(self, oper: OP) -> None:
        """Process a newly tagged operator for externalization."""
        # Route TDN-tagged COMPs to the TDN handler
        if oper.family == 'COMP' and self.my.par.Tdntag.val in oper.tags:
            self._handleTDNAddition(oper)
            return

        # Nothing is written to disk before the project has a real home:
        # paths root at project.folder, which is TD's default location on
        # a never-saved project (the fresh-drop externalizations.tsv
        # orphan). The op stays tagged-but-untracked, so the additions
        # sweep re-detects it on every Update and externalization
        # self-heals with the first sweep after the save.
        if not self._projectSavedOnDisk():
            self.Log(f"Deferring externalization of '{oper.path}' until "
                     "the project is saved", "DEBUG")
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
        # Same save gate as handleAddition (direct callers exist).
        if not self._projectSavedOnDisk():
            self.Log(f"Deferring externalization of '{oper.path}' until "
                     "the project is saved", "DEBUG")
            return
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
            # Roll back the just-applied tag: a tagged-but-untracked COMP is
            # a dead end -- applyTagToOperator no-ops while the tag is
            # present, so every retry would silently do nothing until the
            # user strips the tag by hand.
            oper.tags.discard(self.my.par.Tdntag.val)
            self.Log(
                f"TDN export failed for {oper.path}: {result.get('error')} "
                f"-- tag rolled back, fix the error and re-tag to retry",
                "ERROR")

    def _cascadeTDNTag(self, parent_comp: OP) -> None:
        """Auto-tag direct child COMPs for TDN externalization.

        Uses depth=1 (direct children only). Recursion happens naturally
        through the applyTagToOperator -> _handleTDNAddition ->
        _cascadeTDNTag chain, processing each level in order.
        """
        tdn_tag = self.my.par.Tdntag.val
        for child in parent_comp.findChildren(type=COMP, depth=1):
            # Annotations never cascade: they are captured semantically by
            # the parent's annotations: section, and tagging one turns its
            # TD-managed widget internals into bogus per-op boundaries.
            # (applyTagToOperator also refuses them -- this skip just keeps
            # the cascade quiet instead of logging a refusal per annotate.)
            if child.type == 'annotate':
                continue
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
        
        # Save file. Issue #86: this is the FIRST .tox for a newly tagged COMP,
        # and it serializes the whole subtree exactly like Save() does. The
        # canonical agent workflow reaches it with Embot inside -- create a
        # container, build ops in it (queued-batch evidence commits him there),
        # then externalize_op the container -> handleAddition -> here.
        save_path_str = str(save_file_path)
        self._retireVizBeforeWrite(oper.path)
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
        """Add or update operator entry in externalizations table.

        The dirty ARG is accepted for caller compatibility but routed to
        the runtime store -- the tsv's dirty column stays blank by
        contract (runtime-only since 2026-08-20)."""
        self._setDirtyState(oper.path, dirty)
        dirty = ''
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

    def _positionCells(self, oper: 'OP') -> dict:
        """The row's position/color cells, or {} on a table without them.

        Returned rather than written so a caller already updating the row can
        MERGE them into its own single write. Every save path was otherwise
        paying a second full DAT-to-file sync (see _updateRowCells) purely to
        record three cells it was about to touch anyway.
        """
        if self.Externalizations is None or (
                self.Externalizations[0, 'node_x'] is None):
            return {}
        c = oper.color
        return {
            'node_x': str(int(oper.nodeX)),
            'node_y': str(int(oper.nodeY)),
            'node_color': f'{c[0]:.4f},{c[1]:.4f},{c[2]:.4f}',
        }

    def _updatePositionInTable(self, oper: 'OP', op_path: str,
                               strategy: str = '') -> None:
        """Write position/color metadata on its own (callers not already
        updating the row -- otherwise merge _positionCells into their dict)."""
        cells = self._positionCells(oper)
        if cells:
            self._updateRowCells(op_path, cells, strategy=strategy)

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
        # 'is None' checks throughout: truthiness on a Par EVALUATES it, so
        # a user par named 'Build' holding 0 (or a broken expression) would
        # wrongly append a duplicate (or raise) under 'if not par'.
        build_par = next((p for p in oper.customPars if p.name == 'Build'), None)
        if build_par is None:
            build_par = build_page.appendInt('Build', label='Build Number')
            build_par.readOnly = True
        build_par.val = build_num

        # Date
        date_par = next((p for p in oper.customPars if p.name == 'Date'), None)
        if date_par is None:
            date_par = build_page.appendStr('Date', label='Build Date')
            date_par.readOnly = True
        date_par.val = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        # Touch Build
        touch_par = next((p for p in oper.customPars if p.name == 'Touchbuild'), None)
        if touch_par is None:
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
            embody_root = self.my.path
            for i in range(1, self.Externalizations.numRows):
                row_path = self._cellVal(i, 'path')
                if row_path:
                    # HARD INVARIANT: the continuity sweep must NEVER touch
                    # Embody's own subtree. Its externalization is managed
                    # specially (excluded from TDN strip/reconstruction). During
                    # heavy strip/restore thrashing these rows can transiently
                    # look "missing"/"replaced" and get deleted or re-externalized
                    # (observed: a TDN->TOX flip that destroyed Embody's own .tdn
                    # files). Skipping them here closes that gap; the normal case
                    # was already a no-op, so nothing legitimate is lost.
                    if (row_path == embody_root
                            or row_path.startswith(embody_root + '/')):
                        continue
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
                    # A legacy row AT an annotation is an inert pre-guard
                    # artifact, NOT a vanished operator. Bare op() cannot
                    # resolve a UTILITY annotateCOMP itself (the utility flag
                    # hides the node from its parent's lookup -- measured on
                    # TD 099.2025.33070; interiors DO still resolve), so
                    # without this check the sweep concluded the COMP was
                    # deleted and either queued the file-cleanup modal or,
                    # with Filecleanup='delete', silently unlinked the .tdn
                    # and dropped the row -- on EVERY Update()/save.
                    # _isAnnotateInteriorPath has the utility-aware walk that
                    # answers this correctly; the sweep just never asked.
                    if self._isAnnotateInteriorPath(old_op_path):
                        continue
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

    # Dropped-.tox prompt: cap the paths listed in the dialog body so a project
    # with dozens/hundreds of dragged-in COMPs cannot grow the message so tall
    # that the buttons get pushed off-screen. Overflow collapses to "... and N
    # more". Mirrors the truncation in _warnLockedNonDATs.
    _MAX_TOXDROP_LISTED = 15

    def _resetToxdropExpr(self, comp) -> None:
        """Clear TD's default drag-in expression from a COMP's External .tox."""
        try:
            comp.par.externaltox.expr = ''
            comp.par.externaltox = ''
            self.Log(f"Reset externaltox for '{comp.path}'", "SUCCESS")
        except Exception as e:
            self.Log(f"Error resetting '{comp.path}'", "ERROR", str(e))

    def _checkExternalToxPar(self):
        """Handle COMPs carrying TD's default drag-in externaltox expression.

        When a .tox is dragged into a network, TouchDesigner auto-writes a
        default expression into the COMP's External .tox parameter
        (``me.parent().fileFolder + '/' + ...``). Embody's own descendants are
        always cleaned silently -- that expression there is never intentional.
        User COMPs are routed to _resolveToxdropExternals, which applies the
        ``Toxdropexpr`` preference (clean / ignore / ask).

        COMPs opted out of Embody via the exclude tag (on themselves OR any
        ancestor -- the tag marks a whole app-managed subtree) are left
        alone entirely: not listed, not prompted about, not cleaned (issue
        #60: users tag their startup-file COMPs tdn_exclude precisely so
        Embody never touches them).
        """
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
        exclude_tag = self.my.par.Tdnexcludetag.eval()
        internal, external = [], []
        for comp in comps_with_filefolder:
            if comp.path == embody_path or comp.path.startswith(embody_path + '/'):
                internal.append(comp)
            elif self._hasExcludeTagInAncestry(comp, exclude_tag):
                continue
            else:
                external.append(comp)

        # Embody's own descendants: always clean, regardless of preference.
        for comp in internal:
            self._resetToxdropExpr(comp)

        self._resolveToxdropExternals(external)

    def _hasExcludeTagInAncestry(self, comp, exclude_tag=None) -> bool:
        """True if comp or any ancestor COMP carries the TDN exclude tag.

        Broader than TDNExt._hasExcludeTag (which checks only the op's own
        tags, per the TDN direct-child-of-boundary contract): sweeps like
        the dropped-.tox check must honor the tag for the WHOLE tagged
        subtree, since users tag a root COMP intending "Embody, leave all
        of this alone".

        exclude_tag: pass the pre-evaluated tag when calling in a loop
        (e.g. the dropped-.tox sweep) to avoid re-evaluating the parameter
        per COMP.
        """
        if exclude_tag is None:
            exclude_tag = self.my.par.Tdnexcludetag.eval()
        if not exclude_tag:
            return False
        o = comp
        while o is not None and o.path != '/':
            if o.isCOMP and o.type != 'annotate' and exclude_tag in o.tags:
                return True
            o = o.parent()
        return False

    def _resolveToxdropExternals(self, external) -> None:
        """Apply the Toxdropexpr preference to a list of user COMPs carrying
        TD's default drag-in externaltox expression.

          - ``clean``  -> silently clear the expression on each
          - ``ignore`` -> silently leave them (never prompt)
          - ``ask``    -> prompt with a truncated list (capped at
                          _MAX_TOXDROP_LISTED so the buttons stay reachable) and
                          a 4-button dialog: Clean / Ignore / Always Clean /
                          Always Ignore. The two "Always" buttons persist the
                          choice into ``Toxdropexpr`` so the user is not
                          re-prompted. A plain Ignore is remembered for the
                          session (``_toxdrop_ignored_session``) so subsequent
                          sweeps don't re-ask about the same COMPs (issue #60);
                          a dismissed/suppressed dialog (-1) is NOT remembered
                          and re-offers on the next pass.

        Operates ONLY on the COMPs passed in -- never re-scans the project --
        so callers (and tests) control the blast radius.
        """
        if not external:
            return

        par = getattr(self.my.par, 'Toxdropexpr', None)
        preference = par.eval() if par else 'ask'

        if preference == 'ignore':
            return
        if preference == 'clean':
            for comp in external:
                self._resetToxdropExpr(comp)
            return

        # preference == 'ask' -- prompt. _messageBox self-suppresses during
        # saves and test runs (returning -1), so no modal escapes; when
        # suppressed we do nothing and the sweep re-offers on the next pass.
        # COMPs the user already answered plain-Ignore for THIS SESSION are
        # dropped up front: every Refresh sweep re-runs this check, and
        # re-asking about the same COMPs each sweep is the issue #60
        # nagging loop. A new drop (new path) still prompts; the set
        # resets on extension reinit / project reload.
        ignored = getattr(self, '_toxdrop_ignored_session', set())
        external = [c for c in external if c.path not in ignored]
        if not external:
            return

        count = len(external)
        shown = external[:self._MAX_TOXDROP_LISTED]
        op_list = '\n'.join(f'  - {comp.path}' for comp in shown)
        if count > self._MAX_TOXDROP_LISTED:
            op_list += f'\n  ... and {count - self._MAX_TOXDROP_LISTED} more'
        noun = 'COMP' if count == 1 else 'COMPs'
        verb = 'uses' if count == 1 else 'use'
        message = (
            f'{count} {noun} {verb} the default expression TouchDesigner writes '
            f"into External .tox on drag-in (me.parent().fileFolder + '/' + "
            f'...):\n\n{op_list}\n\n'
            f'Clean clears that expression; Ignore leaves it. '
            f'Choose an "Always" option to stop being asked.')
        choice = self._messageBox(
            'Dropped .tox Expression Detected',
            message,
            buttons=['Clean', 'Ignore', 'Always Clean', 'Always Ignore'])

        # 0 Clean, 1 Ignore, 2 Always Clean, 3 Always Ignore, -1 suppressed/closed.
        if choice in (0, 2):
            for comp in external:
                self._resetToxdropExpr(comp)
        elif choice == 1:
            # Plain Ignore: honor it for the rest of the session instead
            # of re-prompting on every subsequent sweep (issue #60).
            # 'Always Ignore' persists via Toxdropexpr; this is the
            # lighter, session-scoped variant.
            self._toxdrop_ignored_session = (
                ignored | {c.path for c in external})
        if choice == 2 and par:
            self.my.par.Toxdropexpr = 'clean'
            self.Log('Dropped .tox expression handling set to Always Clean',
                     'INFO')
        elif choice == 3 and par:
            self.my.par.Toxdropexpr = 'ignore'
            self.Log('Dropped .tox expression handling set to Always Ignore',
                     'INFO')

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
                    # replace(), not rename(): when the target already exists
                    # (Embody's own sweep can export the new-name .tdn before
                    # this continuity path runs), rename() overwrites on
                    # POSIX but raises FileExistsError on Windows -- leaking
                    # the old .tdn on every rename (issue #57 follow-up,
                    # observed as 'Error renaming TDN file' in test runs).
                    old_abs.replace(new_abs)
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
        # Never open the file-cleanup modal while dialogs are suppressed -- a
        # test run OR a project save in progress (_suppressDialogs). A save's
        # post-save Refresh reaches checkOpsForContinuity while _suppress_dialogs
        # is still set; a modal mid-save freezes TD, and ops that are only
        # transiently "missing" (mid strip/restore) get re-evaluated by the next
        # continuity check once suppression lifts. Reads the save flag from
        # storage, so it protects user .tox projects too (no test runner needed).
        if self._suppressDialogs():
            self.Debug('File-cleanup continuity dialog suppressed (test/save '
                       f'active, {len(missing_ops)} missing ops)')
            return

        # Even when not suppressed, filter out transient test-sandbox ops that a
        # between-suite reinit can surface as "missing" (covers the standard
        # sandbox COMP and root-level test sandboxes like /_test_dat_restore).
        try:
            runner = getattr(op, 'unit_tests', None)
            if runner:
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
            choice = self._messageBox(
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

            # Save to new location. Issue #86: the rename re-save serializes the
            # whole subtree too, and a rename is precisely the event that
            # invalidates viz's own path bookkeeping -- so the retire's
            # structural subtree sweep is what actually catches him here.
            self._retireVizBeforeWrite(new_op.path)
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
        """Remove all duplicate rows in the externalizations table.

        ONE pass over the table, grouping rows by (path, type) as it goes,
        then deleting the stale members of any group holding more than one
        row. Same keep-the-most-recent-per-type semantics as
        cleanupDuplicateRows, which this deliberately does NOT call per
        path: that helper re-scans the WHOLE table for every path it is
        given, which made this O(rows x paths) -- 182k guarded cell reads
        and ~500ms of BLOCKED MAIN THREAD on a 300-row project, on every
        Refresh, so on every save. None of this work can move off the main
        thread (it reads a DAT), so it has to be cheap instead.
        """
        self._dedupeRows()

    def _dedupeRows(self, only_path: Optional[str] = None) -> dict:
        """Remove duplicate rows in ONE table pass; return each group's keeper.

        The single implementation behind both cleanupAllDuplicateRows() and
        cleanupDuplicateRows(): the rule (keep the most recent row per
        externalization) existed twice, in two shapes, so any change to
        tie-breaking had to land in both.

        Groups by (path, STRATEGY) -- not (path, type). A COMP may legitimately
        hold both a TOX row and a TDN row, and `type` holds the OP type
        ('base'/'container'), identical on both, while `strategy` is what
        distinguishes them. Keying on `type` put a legitimate pair in ONE group
        and silently deleted the older row, destroying a tracking row and its
        recovery pointer. `type` remains the key only for legacy tables that
        predate the strategy column, where it did hold 'tox'/'tdn'.

        Args:
            only_path: restrict to one op path (the per-path callers); None
                sweeps the whole table.

        Returns:
            {(path, kind): kept_row_index} in table order.
        """
        table = self.Externalizations
        if table is None:
            return {}
        kind_col = 'strategy' if table[0, 'strategy'] is not None else 'type'
        groups = {}
        for i in range(1, table.numRows):
            path = self._cellVal(i, 'path', table=table)
            if not path or (only_path is not None and path != only_path):
                continue
            groups.setdefault(
                (path, self._cellVal(i, kind_col, table=table)), []).append(i)

        def _stamp(i):
            """Parse a row's timestamp. Deferred until a group is KNOWN to hold
            duplicates -- parsing every row cost 302 strptime calls per Refresh
            on a project where almost no row has a duplicate."""
            try:
                ts_str = self._cellVal(i, 'timestamp', table=table)
                return (datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC")
                        if ts_str else datetime.min)
            except (ValueError, TypeError) as e:
                self.Log(f"Failed to parse timestamp for row {i}: {e}", "DEBUG")
                return datetime.min

        # Collect every stale row first, then delete highest index -> lowest so
        # the shifting row indices can never invalidate a pending deletion.
        kept, stale = {}, []
        for key, rows in groups.items():
            keep = rows[0] if len(rows) == 1 else max(rows, key=_stamp)
            kept[key] = keep
            stale.extend((i, key[0], key[1]) for i in rows if i != keep)

        for i, path, kind in sorted(stale, reverse=True):
            table.deleteRow(i)
            self.Log(f"Removed duplicate row {i} for {path} ({kind})", "INFO")
            # Deleting shifts every higher index down by one.
            for key, row in kept.items():
                if row > i:
                    kept[key] = row - 1
        return kept

    def cleanupDuplicateRows(self, path: str) -> Optional[int]:
        """Remove duplicate rows for ONE path; return the surviving row index.

        A COMP can legitimately have both a TOX row and a TDN row -- different
        externalizations, not duplicates. Shares its implementation with
        cleanupAllDuplicateRows via _dedupeRows so the keep-the-most-recent
        rule (and the tie-break) exists in exactly one place.

        When a path holds several externalizations the index of the LAST one in
        table order is returned, matching the previous behaviour.
        """
        kept = self._dedupeRows(only_path=path)
        return list(kept.values())[-1] if kept else None

    def _buildPathGroups(self) -> dict:
        """Map normalized external paths to lists of operators sharing them.

        Only includes operators with Embody tags that are not inside
        TD clone hierarchies or replicator outputs.
        """
        # Set intersection, not `any(tag in oper.tags ...)`: the latter probes
        # the TD tag store once PER KNOWN TAG per operator (~20x), which
        # profiled as 19,813 TDStoreTools.__contains__ calls in a single
        # Refresh. Reading oper.tags once and intersecting is identical in
        # meaning and reads the store once.
        embody_tags = set(self.getTags())
        path_groups = {}

        for oper in self.root.findChildren(type=COMP, parName='externaltox'):
            if not (embody_tags & set(oper.tags)):
                continue
            if self.isInsideClone(oper) or self.isReplicant(oper):
                continue
            path = self.normalizePath(oper.par.externaltox.eval())
            if path:
                path_groups.setdefault(path, []).append(oper)

        for oper in self.root.findChildren(type=DAT, parName='file'):
            if not (embody_tags & set(oper.tags)):
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
            self._messageBox('Embody Error', f"'{oper.type}' type not supported.", buttons=['Ok'])
            return

        if self.isReplicant(oper) or self.isClone(oper) or self.isInsideClone(oper):
            self._messageBox('Embody Warning', 
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
            self._messageBox('Embody Error',
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
            # Annotations and their internals are never taggable -- same
            # refusal as applyTagToOperator (the tagger UI routes here, not
            # through the chokepoint). The REMOVE branch below stays
            # unguarded so legacy-tagged annotates can always be cleaned up.
            if (oper.family == 'COMP' and oper.type == 'annotate') \
                    or self._isInsideAnnotate(oper):
                self.Log(
                    f"Refusing to tag '{oper.path}': annotations and their "
                    f"internals are captured semantically by the parent TDN "
                    f"COMP's annotations: section, never externalized per-op",
                    'WARNING')
                return False
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
            choice = self._messageBox(
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
            # Utility-aware: a bare op() here reported LIVE children of a
            # utility annotate as gone, dropping their rows while leaving
            # their .tdn files on disk as stranded, untracked orphans.
            if child_path.startswith(prefix) \
                    and not self.resolveOpIncludingUtility(child_path):
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

    def _isInsideAnnotate(self, oper: OP) -> bool:
        """True when any ancestor COMP of oper is an annotateCOMP.

        Annotate widget internals (the annotation/back/body/title/...
        containers and their color/i/help tables) are TD-managed stock
        content -- never Embody's to tag, externalize, or track. The
        annotation itself round-trips exclusively through the parent
        TDN's semantic `annotations:` section."""
        try:
            node = oper.parent() if oper is not None else None
            while node is not None and node.path != '/':
                if node.type == 'annotate':
                    return True
                node = node.parent()
        except Exception:
            pass
        return False

    def resolveOpIncludingUtility(self, op_path: str) -> Optional[OP]:
        """op() that can also reach UTILITY children (annotateCOMPs).

        Bare op() cannot resolve a utility annotateCOMP itself -- the
        utility flag hides the node from its parent's lookup and from a
        plain findChildren (measured on TD 099.2025.33070; paths THROUGH
        a utility annotate still resolve, only the node itself does not).

        Removal primitives must use this: with bare op() returning None,
        RemoveListerRow / RemoveTDNEntry dropped the table row but never
        stripped the operator's tag, colour, or _tdn_rel_path breadcrumb,
        so the next Refresh sweep resurrected the row -- the silent no-op
        reported against v6.0.157. Returns None when the path genuinely
        does not resolve."""
        # Input guard, matching envoy_read.resolve_op. Without it the walk
        # below turns '' into ['']  -> every segment skipped -> the PROJECT
        # ROOT is returned where bare op('') gives None, and a RELATIVE
        # path is silently re-rooted at '/'. Both feed destructive
        # primitives that strip tags, clear externaltox, force-cook and
        # recolour -- doing that to '/' would maul the whole project.
        if not op_path or not isinstance(op_path, str):
            return None
        op_path = op_path.rstrip('/') or '/'
        if not op_path.startswith('/'):
            return None
        if op_path == '/':
            return op('/')
        try:
            target = op(op_path)
            if target is not None:
                return target
            node = op('/')
            for seg in op_path.split('/'):
                if not seg:
                    continue
                if node is None or not node.isCOMP:
                    return None
                nxt = node.op(seg)
                if nxt is None:
                    nxt = next(
                        (c for c in node.findChildren(
                            depth=1, includeUtility=True)
                         if c.name == seg), None)
                node = nxt
            return node
        except Exception:
            return None

    def _annotateRootForPath(self, op_path: str) -> Optional[str]:
        """Path of the annotateCOMP that op_path IS or lives inside.

        Used to collapse per-row warnings into one line per annotation.
        Returns None when the path is not annotation-related or the live
        network cannot testify."""
        try:
            target = self.resolveOpIncludingUtility(op_path)
            if target is None:
                return None
            if target.type == 'annotate':
                return target.path
            node = target.parent()
            while node is not None and node.path != '/':
                if node.type == 'annotate':
                    return node.path
                node = node.parent()
        except Exception:
            pass
        return None

    def _isAnnotateInteriorPath(self, op_path: str) -> bool:
        """True when op_path IS an annotateCOMP or lies inside one,
        resolved against the LIVE network (utility-aware).

        Legacy externalization rows pointing AT an annotation or inside
        its widget (created before the annotate tagging guards existed)
        must be inert: reconstructing them guts the widget's stock
        internals (empty color tables -> float(None) cook errors) and
        re-exporting them recreates orphan files. Bare op() cannot see a
        utility annotate hop, so on lookup failure walk the path down
        with a findChildren(includeUtility=True) fallback and stop at
        the first annotate segment (leaf included -- both branches must
        agree regardless of the utility flag). An unresolvable path
        returns False -- when the live network cannot testify, behave as
        before."""
        try:
            target = op(op_path)
            if target is not None:
                return (target.type == 'annotate'
                        or self._isInsideAnnotate(target))
            node = op('/')
            for seg in op_path.split('/'):
                if not seg:
                    continue
                if not node.isCOMP:
                    return False
                nxt = node.op(seg)
                if nxt is None:
                    nxt = next(
                        (c for c in node.findChildren(
                            depth=1, includeUtility=True)
                         if c.name == seg), None)
                if nxt is None:
                    return False
                if nxt.type == 'annotate':
                    return True
                node = nxt
        except Exception:
            pass
        return False

    def applyTagToOperator(self, oper: OP, tag: str) -> bool:
        """Apply a tag to an operator. Enforces mutual exclusivity.

        Annotations are refused wholesale: an annotateCOMP round-trips
        exclusively through the parent TDN's semantic `annotations:`
        section, and its internals are TD-managed stock widgetry --
        tagging either creates per-op boundaries whose reconstruction
        guts the widget and strands orphan files."""
        if (oper.family == 'COMP' and oper.type == 'annotate') \
                or self._isInsideAnnotate(oper):
            self.Log(
                f"Refusing to tag '{oper.path}': annotations and their "
                f"internals are captured semantically by the parent TDN "
                f"COMP's annotations: section, never externalized per-op",
                'WARNING')
            return False
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

    # ==========================================================================
    # AUTO-EXTERNALIZATION (Envoy-created ops)
    # ==========================================================================

    def AutoExternalizeNewOp(self, oper: OP) -> Optional[str]:
        """Auto-tag a newly Envoy-created op for externalization, per the
        'Autoexternalize' preference on the Envoy page.

        Called by Envoy's create_op (the single creation chokepoint) right
        after a successful create. This is deliberately the ONLY trigger: the
        LLM is never asked to "remember to externalize" -- tagging rides along
        on the create it already performs, so an unreliable model cannot skip a
        step it never takes.

        Strictly ADDITIVE and boundary-scoped. It only ever ADDS a tag (never
        deletes a file or un-tags), and it tags a COMP/DAT only when that op is
        its own externalization unit:

          - the preference gates the family (Neither / DATs / COMPs / both)
          - COMP -> TDN tag (the diffable strategy); DAT -> inferred source tag
          - skip non-processable ops (clone/replicant/local/engine/time/annotate)
          - skip Embody's own subtree (managed specially, never externalized here)
          - skip palette/vendor clones and anything inside one
          - skip if ANY ancestor COMP is already externalized -- that ancestor's
            .tdn/.tox already captures this op (outermost boundary wins), which
            also keeps us clear of the TDN parent/child collision

        The write is handled by the existing reconciler: applying the TDN tag
        exports the .tdn synchronously (_handleTDNAddition); a loose DAT's file
        is written by a single coalesced, settle-debounced Update() so a batch
        of creates costs one sweep, not one per op. Returns the applied tag
        string, or None if the op was skipped. Never raises -- a failure here
        must never break op creation.
        """
        try:
            tag = self._autoExternalizeTagFor(oper)
            if not tag:
                return None
            if not self.applyTagToOperator(oper, tag):
                # Chokepoint refused (annotate guard / no color for tag) --
                # report untagged rather than logging a false success.
                return None
            # COMPs export synchronously inside _handleTDNAddition. A loose DAT's
            # file is written by the addition sweep -- coalesce that into one
            # settle-debounced Update() so a batch of DAT creates costs one sweep.
            if oper.family == 'DAT':
                self._scheduleAutoExternalizeFlush()
            self.Log(f"Auto-externalized {oper.family} '{oper.path}' ({tag})", "INFO")
            return tag
        except Exception as e:
            self.Log(f"AutoExternalizeNewOp failed for "
                     f"{getattr(oper, 'path', '?')}: {e}", "WARNING")
            return None

    def _autoExternalizeTagFor(self, oper: OP) -> Optional[str]:
        """Pure decision behind AutoExternalizeNewOp: the tag that WOULD be
        applied to a newly-created `oper` under the current 'Autoexternalize'
        preference, or None if the op should be skipped. No side effects -- the
        whole boundary matrix is unit-testable without any file I/O.

        Skips (return None): preference off / family not selected; a
        non-externalizable family; a non-processable op; Embody's own subtree; a
        palette/vendor clone (the op or any ancestor); an op with any already-
        externalized ancestor COMP (that ancestor's .tdn/.tox already captures
        it); or an op that already carries the tag (idempotent).
        """
        if oper is None or not oper.valid:
            return None

        mode = self.my.par.Autoexternalize.eval()
        dats_on = mode in ('dats', 'both')
        comps_on = mode in ('comps', 'both')

        # Family gate -- only COMPs and DATs are externalizable at all.
        if oper.family == 'COMP':
            if not comps_on:
                return None
        elif oper.family == 'DAT':
            if not dats_on:
                return None
        else:
            return None

        # Never externalize non-processable ops or Embody's own subtree.
        if not self.isOpProcessable(oper):
            return None
        embody_root = self.my.path
        if oper.path == embody_root or oper.path.startswith(embody_root + '/'):
            return None

        # Skip palette/vendor clones -- the op itself or any COMP ancestor
        # (a clone's internals are regenerable palette boilerplate) -- and
        # anything inside an annotateCOMP (widget internals are TD-managed;
        # isOpProcessable above only catches the annotate ITSELF).
        node = oper if oper.family == 'COMP' else oper.parent()
        while node is not None and node.path != '/':
            if node.family == 'COMP' and (
                    node.type == 'annotate'
                    or self.my.ext.TDN._isPaletteClone(node)):
                return None
            node = node.parent()

        # Boundary: if any ancestor COMP is already externalized, this op is
        # already captured by that ancestor's .tdn/.tox -- don't double-manage
        # (and don't collide with the TDN parent/child model).
        tox_tag = self.my.par.Toxtag.val
        tdn_tag = self.my.par.Tdntag.val
        ancestor = oper.parent()
        while ancestor is not None and ancestor.path != '/':
            if (tox_tag in ancestor.tags or tdn_tag in ancestor.tags
                    or self._findExternalizedComp(ancestor.path)):
                return None
            ancestor = ancestor.parent()

        # COMP -> TDN (diffable); DAT -> inferred source type.
        tag = tdn_tag if oper.family == 'COMP' else self._inferDATTagValue(oper)

        # Idempotent: already carries this tag -> nothing to do.
        if tag in oper.tags:
            return None
        return tag

    def _scheduleAutoExternalizeFlush(self) -> None:
        """Coalesce loose-DAT externalization into one settle-debounced Update().
        Multiple auto-tagged DATs in a batch collapse to a single sweep."""
        if getattr(self, '_auto_ext_flush_pending', False):
            return
        self._auto_ext_flush_pending = True
        run(f"op('{self.my}').ext.Embody._autoExternalizeFlush()",
            delayFrames=8, fromOP=self.my)

    def _autoExternalizeFlush(self) -> None:
        """Deferred reconcile that writes newly auto-tagged DAT files. Re-arms
        itself while a save is mid-flight (mutating the table then is fatal)."""
        self._auto_ext_flush_pending = False
        if self._performMode:
            return
        if self.my.fetch('_suppress_dialogs', False, search=False):
            self._scheduleAutoExternalizeFlush()  # save window -- wait it out
            return
        self.Update()

    def AutoExternalizeCopiedOp(self, oper: OP) -> Optional[str]:
        """Auto-externalize a COPIED op (copy_op), per the Autoexternalize
        preference. A copy made with COMP.copy() inherits the SOURCE's
        externalization tags -- and a copied DAT inherits its `file` par pointing
        at the SOURCE's file. That stale state must be cleared first, otherwise
        the copy looks 'already tagged' (so it is skipped) yet is untracked with
        no file of its own, and a copied DAT would share/overwrite the source's
        .py via syncfile. So: gate on preference+family, clear the copy's
        inherited externalization state (recursively for a COMP, so its TDN
        export is fully self-contained and references none of the source's
        files), then defer to the normal fresh-op path. Never raises."""
        try:
            if oper is None or not oper.valid:
                return None
            mode = self.my.par.Autoexternalize.eval()
            if oper.family == 'COMP' and mode not in ('comps', 'both'):
                return None
            if oper.family == 'DAT' and mode not in ('dats', 'both'):
                return None
            if oper.family not in ('COMP', 'DAT'):
                return None
            self._resetInheritedExternalization(oper)
            return self.AutoExternalizeNewOp(oper)
        except Exception as e:
            self.Log(f"AutoExternalizeCopiedOp failed for "
                     f"{getattr(oper, 'path', '?')}: {e}", "WARNING")
            return None

    def _resetInheritedExternalization(self, oper: OP) -> None:
        """Clear externalization tags + file references a COPY inherited from its
        source, so it externalizes fresh at its OWN path and never points at the
        source's files. Recurses through a copied COMP's descendants so a TDN
        export captures live content only (no stale source-file references)."""
        tox = self.my.par.Toxtag.val
        tdn = self.my.par.Tdntag.val
        dat_tag_set = set(self.getTags('DAT'))

        def clear(o):
            try:
                if o.family == 'COMP':
                    for t in (tox, tdn):
                        if t in o.tags:
                            o.tags.remove(t)
                    p = getattr(o.par, 'externaltox', None)
                    if p is not None and p.eval():
                        p.val = ''
                elif o.family == 'DAT':
                    for t in list(o.tags):
                        if t in dat_tag_set:
                            o.tags.remove(t)
                    fp = getattr(o.par, 'file', None)
                    if fp is not None:
                        fp.readOnly = False
                        if fp.eval():
                            fp.val = ''
            except Exception:
                pass

        clear(oper)
        if oper.family == 'COMP':
            for child in oper.findChildren(maxDepth=100):
                if child.family in ('COMP', 'DAT'):
                    clear(child)

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

        # A refused switch (e.g. the annotate guard) keeps the OLD tag --
        # bail out entirely so ExternalizeImmediate does not re-export the
        # file under the old strategy that the refusal meant to keep inert.
        if tox_tag in oper.tags:
            if not self.applyTagToOperator(oper, tdn_tag):
                return
        elif tdn_tag in oper.tags:
            if not self.applyTagToOperator(oper, tox_tag):
                return

        self.ExternalizeImmediate(oper)
        self.Refresh()

    def HandleStrategySave(self, oper: OP) -> None:
        """Save the current strategy for a COMP."""
        tox_tag = self.my.par.Toxtag.val
        tdn_tag = self.my.par.Tdntag.val

        if tox_tag in oper.tags:
            # allow_empty: this is the one EXPLICIT save gesture, so a
            # deliberately emptied COMP may overwrite its file here (the
            # automatic writers refuse that shape as data loss).
            self.Save(oper.path, allow_empty=True)
        elif tdn_tag in oper.tags:
            self.SaveTDN(oper.path, allow_empty=True)
        else:
            # Fallback: check externalizations table for untagged COMPs (e.g. root)
            strategy = self._getCompStrategy(oper)
            if strategy == 'tox':
                self.Save(oper.path, allow_empty=True)
            elif strategy == 'tdn':
                self.SaveTDN(oper.path, allow_empty=True)

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

        result = self._messageBox(
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
            tdn_doc = self.my.ext.TDN.tdn_load(abs_path.read_text(encoding='utf-8'))
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
            # Re-baseline dirty-detection for the root AND every tracked
            # TDN COMP inside it: the import just made live == disk, and
            # a stale pre-reload fingerprint reads the fresh content as
            # dirty -- the vector that let an auto-export overwrite a
            # nested child's .tdn from its transiently-empty shell
            # (field data loss, 2026-08-12; the shells themselves are now
            # filled by import Phase 8.6).
            try:
                tdn_paths = self._getTDNPaths()
                exclude_tag = self.my.par.Tdnexcludetag.eval()
                self._storeTDNFingerprint(oper, tdn_paths, exclude_tag)
                prefix = oper.path.rstrip('/') + '/'
                for comp_path, _rel in self._getTDNStrategyComps():
                    if comp_path.startswith(prefix):
                        nested = op(comp_path)
                        if nested is not None:
                            self._storeTDNFingerprint(
                                nested, tdn_paths, exclude_tag)
                self.param_tracker.updateParamStore(oper)
            except Exception as e:
                self.Log(f'Reload re-baseline failed for {oper.path}: '
                         f'{e}', 'WARNING')

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

        # enableexternaltoxpulse is the only working reload trigger on TD
        # 2025: toggling enableexternaltox off->on does NOT re-read the
        # .tox, and reloadtoxpulse does not exist.
        oper.par.enableexternaltox = True
        oper.par.enableexternaltoxpulse.pulse()
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
        ok = self.ExportPortableTox(target=oper, save_path=str(path))
        self.Refresh()
        if not ok:
            self._messageBox(
                'Export portable tox',
                'Portable export failed -- check the Embody log.\n\n'
                'Possible causes: a pre_release hook abort (no .tox\n'
                'written), a save failure, or a post_release hook\n'
                'failure (the .tox may still exist on disk).',
                buttons=['OK'])

    def HandleStrategyRemove(self, oper: OP) -> None:
        """Remove externalization from a COMP or DAT with confirmation dialog."""
        result = self._messageBox(
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
            # RemoveTDNEntry strips the tags itself (issue #48)
            self.RemoveTDNEntry(oper.path)
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
        # Render the live export binding (remappable; empty when disabled)
        # instead of hardcoding a combo that may be wrong for the platform.
        export_combo = str(self.my.par.Shortcutexportproject.eval()).strip()
        export_hint = (f' ({mod.shortcuts.display(export_combo)})'
                       if export_combo else '')
        choice = self._messageBox('Embody -- Externalize Full Project',
            'Add all compatible COMPs and DATs to Embody?\n'
            '(Palette components, clones, and replicants will be ignored)\n\n'
            '  TOX: Externalize each COMP as a .tox file.\n'
            '  TDN: Externalize each COMP as a .tdn file.\n\n'
            'Optionally, also export a single project-wide .tdn\n'
            f'snapshot of your entire network{export_hint}.',
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
        """Check if operator should be skipped in project externalization.

        The exclude tag is honored for the WHOLE tagged subtree (ancestry
        walk, issue #60): ExternalizeProject iterates a flat findChildren,
        so an own-tag-only check would still tag every untagged descendant
        inside an excluded tree.
        """
        return (
            oper.path in paths_to_exclude or
            # Annotations and their internals never externalize -- captured
            # semantically by the parent TDN. applyTagToOperator would refuse
            # each one anyway (with a WARNING per op); skipping here keeps a
            # project sweep over legacy non-utility annotates quiet.
            oper.type == 'annotate' or
            self._isInsideAnnotate(oper) or
            self.isReplicant(oper) or
            self.isInsideClone(oper) or
            self._hasExcludeTagInAncestry(oper) or
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
            # Utility-aware: bare op() cannot resolve a utility annotateCOMP,
            # so a legacy row AT one used to drop the table row while leaving
            # the tag and colour on the operator -- the next Refresh sweep
            # then resurrected the row (the "removed nothing" no-op reported
            # against v6.0.157).
            oper = self.resolveOpIncludingUtility(op_path)
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
                    # The op at a tracked path may be a non-file-backed DAT
                    # (e.g. a selectDAT after a type swap, issue #54); it has
                    # no file/syncfile pars to clear, and touching them would
                    # abort the color reset and tracker removal below.
                    if hasattr(oper.par, 'file'):
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

    def RemoveTDNEntry(self, op_path: str, delete_file: bool = True) -> None:
        """Remove a TDN strategy entry and delete the .tdn file from disk.

        Also strips the operator's externalization tags, clears the
        `_tdn_rel_path` recovery breadcrumb, resets its color, and drops
        its parameter-tracker entry (mirroring RemoveListerRow).
        Leaving the tdn tag in place turns removal into resurrection: the
        Update sweep that runs on every save re-externalizes any
        tagged-but-untracked COMP, restoring the row and .tdn file the user
        just deleted (issue #48). The breadcrumb must go for the same
        reason: ReconcileMetadata and RecoverOrphanShells treat it as
        tracking truth and would resurrect the row from it. Tolerates a
        missing operator -- Full Project entries track paths (e.g. '/')
        that carry no tag.

        Args:
            delete_file: When False, keep the .tdn on disk (the MCP
                remove_externalization_tag path defaults to this; the
                lister X button keeps the default True).
        """
        try:
            # Utility-aware -- see RemoveListerRow. A legacy row AT a utility
            # annotate resolved to None here, so the tag and the
            # _tdn_rel_path breadcrumb survived the removal and
            # ReconcileMetadata / RecoverOrphanShells rebuilt the row.
            oper = self.resolveOpIncludingUtility(op_path)
            if oper:
                for tag in self.getTags():
                    if tag in oper.tags:
                        oper.tags.remove(tag)
                oper.unstore('_tdn_rel_path')
                self.resetOpColor(oper)
                self.param_tracker.removeComp(op_path)
        except Exception as e:
            self.Log(f"Error handling operator '{op_path}'", "ERROR", str(e))
        self._removeTDNStrategy(op_path, delete_file=delete_file)
        self.lister.reset()

    # ==========================================================================
    # TDN RECONSTRUCTION ON START
    # ==========================================================================

    def ReconstructTDNComps(self) -> None:
        """Reconstruct all TDN-strategy COMPs from .tdn files on project open."""
        # Convoy: registration lives in ConvoyExt's tick, which starts at
        # extension CONSTRUCTION -- and TD constructs extensions lazily. In
        # TDN mode off/export nothing touches the convoy COMP at open, so an
        # enabled node sat 'Disabled' until first incidental access (field
        # 2026-08-19: 18 min dormant after a relaunch). Touch .ext past the
        # TDN window; construction never raises a consent dialog.
        run("o = op(%r)\n"
            "if o and o.valid and o.par.Convoyenable.eval() and o.op('convoy'):\n"
            "    o.op('convoy').ext.ConvoyExt" % (self.my.path,),
            delayFrames=15)
        # Fingerprint baselines persisted into the .toe reflect the LAST
        # SAVE's network; the network is freshly restored now, so drop them
        # and let the first sweep re-seed against the current live state.
        self.my.unstore('_tdn_fingerprints')
        mode = self._tdnMode()
        if mode == 'off':
            self.Log('TDN mode=off -- skipping reconstruction', 'INFO')
            return
        if not self.my.par.Tdncreateonstart.eval():
            return
        if mode == 'export':
            # .toe is the source of truth for COMPs that EXIST in it, so we do
            # NOT repopulate those. But a COMP ABSENT from the .toe (e.g. an agent
            # built + autosave-checkpointed it, then crashed before any save) has
            # no .toe truth to honor -- rebuild it from its .tdn. Additive: never
            # clear_first an existing COMP. tsv-driven, so an orphan .tdn with no
            # row is invisible. (Spike-verified 2026-06-27.)
            self.Log('TDN mode=export -- additive recovery only, existing '
                     'COMPs kept (no full reconstruction)', 'INFO')
            self._recoverMissingTDNComps()
            return
        # mode == 'full' -- repopulate ALL TDN COMPs (loop below)

        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return

        self.Log(f'Reconstructing {len(tdn_comps)} TDN COMP(s)...', 'INFO')
        errors_total = 0

        for comp_path, rel_tdn_path in tdn_comps:
            # Re-check per row: at enumeration time the stripped .toe may
            # not contain a legacy row's annotate yet, so the enumerator's
            # filter can miss it (unresolvable -> not filtered). Parents
            # import first (depth sort), so by this row's turn the annotate
            # is live and testifiable -- skip instead of clear_first-gutting
            # the freshly recreated widget.
            if self._isAnnotateInteriorPath(comp_path):
                self.Log(
                    f'Skipping reconstruction of annotation-interior row '
                    f'{comp_path} (inert legacy artifact -- remove the row '
                    f'in the Embody manager to clear it; the annotation '
                    f'itself is kept)', 'WARNING')
                continue
            abs_path = self.buildAbsolutePath(rel_tdn_path)
            if not abs_path.is_file():
                self.Log(f'TDN file not found: {rel_tdn_path}', 'WARNING')
                continue

            try:
                tdn_doc = self.my.ext.TDN.tdn_load(
                    abs_path.read_text(encoding='utf-8'))
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

            # Import from TDN (phases 1-7 + phase 8 file-link restore).
            # Guarded per-COMP: ImportNetwork returns {'error'} on its own
            # failures, but an unexpected raise here must not abort the
            # WHOLE loop -- one bad file would leave every remaining TDN
            # COMP an empty shell for the session. Convert to an error
            # result so the backup-rollback path below still runs.
            try:
                # restore_tdn_shells=False: THIS loop imports every
                # tracked TDN COMP itself, depth-sorted parents-first --
                # Phase 8.6 filling nested shells here would import each
                # nested COMP twice per project open.
                result = self.my.ext.TDN.ImportNetwork(
                    target_path=comp_path,
                    tdn=tdn_doc,
                    clear_first=True,
                    restore_file_links=True,
                    restore_tdn_shells=False,
                )
            except Exception as e:
                result = {'error': f'Import raised: {e}'}

            if result.get('error'):
                self.Log(f'Reconstruction failed for {comp_path}: {result["error"]}', 'ERROR')
                # Attempt rollback from backup .tdn
                try:
                    backup_path = self.my.ext.TDN._get_backup_path_instance(
                        str(abs_path))
                    if backup_path.is_file():
                        backup_tdn = self.my.ext.TDN.tdn_load(
                            backup_path.read_text(encoding='utf-8'))
                        rb_result = self.my.ext.TDN.ImportNetwork(
                            target_path=comp_path, tdn=backup_tdn,
                            clear_first=True, restore_file_links=True,
                            restore_tdn_shells=False)
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

    def _recoverMissingTDNComps(self) -> int:
        """Export-mode auto-save recovery: rebuild TDN COMPs that are tracked and
        have a .tdn on disk but are ABSENT from the just-opened .toe.

        This is the crash-insurance recovery path. An agent that builds +
        autosave-checkpoints a COMP, then crashes before any Ctrl+S, leaves the
        COMP's .tdn + tsv row on disk but the .toe (last saved) lacks the COMP.
        On open we rebuild ONLY those absent COMPs from their .tdn -- additive, so
        it can never clobber a COMP the .toe deliberately carries. tsv-driven (via
        _getTDNStrategyComps, which excludes Embody + ancestors/descendants), so an
        orphan .tdn with no row is invisible -- a deleted COMP is never
        resurrected. Spike-verified 2026-06-27 (children + connections round-trip).

        Returns how many COMPs it rebuilt. The count once fed a startup
        restore bar; the bars view is gone, but the return stays -- it is
        the honest summary a caller or a log line can still report.
        """
        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return 0
        # Snapshot missing-at-start BEFORE any import, so a parent import that
        # creates a child shell mid-pass doesn't make us skip a child still
        # needing its own .tdn populated.
        missing = []
        for comp_path, rel_tdn in tdn_comps:
            if op(comp_path) is not None:
                continue  # exists in the .toe -> .toe is truth, leave it
            abs_path = self.buildAbsolutePath(rel_tdn)
            if abs_path.is_file():
                missing.append((comp_path, abs_path))
        if not missing:
            return 0
        # Parent-before-child so an ancestor shell exists before its children.
        missing.sort(key=lambda m: m[0].count('/'))
        recovered = 0
        for comp_path, abs_path in missing:
            # Same per-row re-check as ReconstructTDNComps: a parent import
            # earlier in this pass may have just recreated the annotate this
            # legacy row points into -- never import into its widget.
            if self._isAnnotateInteriorPath(comp_path):
                self.Log(
                    f'Auto-save recovery: skipping annotation-interior row '
                    f'{comp_path} (legacy artifact)', 'WARNING')
                continue
            try:
                tdn_doc = self.my.ext.TDN.tdn_load(
                    abs_path.read_text(encoding='utf-8'))
                # The op may ALREADY exist -- either as a bare tdn_ref
                # shell, or FULLY POPULATED by a parent import's Phase
                # 8.6 earlier in this pass. A populated one is already
                # identical to its own .tdn (Phase 8.6 imported exactly
                # that file), so re-importing it would be a redundant
                # destroy-and-rebuild -- double extension inits, and a
                # transient failure on the second import would empty a
                # COMP the first had restored (review finding). Only a
                # truly absent or still-empty shell imports here.
                shell = op(comp_path)
                if shell is None:
                    shell = self._createMissingCompShell(
                        comp_path, 'tdn', comp_type_override=tdn_doc.get('type'))
                if shell is None:
                    self.Log(f'Auto-save recovery: cannot create shell for '
                             f'{comp_path} (parent missing?)', 'WARNING')
                    continue
                if any(c.type != 'annotate' for c in shell.children):
                    self.Log(f'Auto-save recovery: {comp_path} already '
                             f'restored by its parent import (Phase '
                             f'8.6); skipping the redundant re-import',
                             'DEBUG')
                    self._storeTDNFingerprint(shell)
                    recovered += 1
                    continue
                res = self.my.ext.TDN.ImportNetwork(
                    target_path=comp_path, tdn=tdn_doc,
                    clear_first=True, restore_file_links=True)
                if res.get('success'):
                    recovered += 1
                    self._reconstructAboutPage(shell, comp_path)
                    self.param_tracker.updateParamStore(shell)
                    self._storeTDNFingerprint(shell)
                else:
                    self.Log(f'Auto-save recovery import failed for {comp_path}: '
                             f'{res.get("error")}', 'ERROR')
            except Exception as e:
                self.Log(f'Auto-save recovery failed for {comp_path}: {e}', 'ERROR')
        if recovered:
            self.Log(f'Auto-save recovery: rebuilt {recovered} unsaved COMP(s) '
                     f'from .tdn (crash-before-save)', 'SUCCESS')
        return recovered

    def RecoverOrphanShells(self, auto: bool = False) -> dict:
        """Detect and restore TDN-tagged empty COMPs that lost their table row.

        The externalizations table is the single driver of reconstruction: a
        stripped COMP whose row is lost (tsv truncation after a crash, a table
        reset) opens as an EMPTY shell and is silently ignored -- its .tdn on
        disk is intact, but tsv-driven recovery never resurrects a no-row
        orphan. This sweep closes that gap using the two breadcrumbs that
        survive inside the .toe itself: the TDN tag on the shell, and the
        `_tdn_rel_path` storage pointer stamped by _trackTDNExport (falling
        back to the mirror-path convention <project>/<comp path>.tdn).

        Additive and consent-gated: only empty, tagged, untracked, non-excluded
        COMPs qualify; nothing with content is ever touched. Runs on project
        open after ReconcileMetadata.

        Args:
            auto: True restores without prompting (tests / headless callers).
                False prompts via _messageBox, which self-suppresses during
                saves and test runs (returns -1 -> deferred to next open).

        Returns:
            {'found': [paths], 'restored': [paths], 'failed': [paths]}
        """
        results = {'found': [], 'restored': [], 'failed': []}
        if self._tdnMode() == 'off':
            return results
        tdn_tag = self.my.par.Tdntag.val
        if not tdn_tag:
            return results

        # Tracked TDN paths -- a rowed COMP is the normal reconstruction
        # path, not an orphan.
        tracked = set()
        table = self.Externalizations
        if table and table[0, 'strategy'] is not None:
            for i in range(1, table.numRows):
                if self._cellVal(i, 'strategy') == 'tdn':
                    tracked.add(self._cellVal(i, 'path'))

        embody_path = self.my.path
        try:
            tagged = self.root.findChildren(type=COMP, tags=[tdn_tag])
        except Exception:
            tagged = []
        candidates = []
        for comp in tagged:
            p = comp.path
            if (p == '/' or p == embody_path
                    or p.startswith(embody_path + '/')
                    or embody_path.startswith(p + '/')):
                continue
            if p in tracked:
                continue
            if self.my.ext.TDN._hasExcludeTag(comp):
                continue
            if comp.findChildren(depth=1):
                continue  # has content -- not a lost shell
            # Locate the .tdn: storage pointer first, then mirror convention.
            rel = None
            try:
                rel = comp.fetch('_tdn_rel_path', None, search=False)
            except Exception:
                rel = None
            abs_path = None
            if rel:
                try:
                    cand = self.buildAbsolutePath(self.normalizePath(rel))
                    if cand.is_file():
                        abs_path = cand
                except Exception:
                    abs_path = None
            if abs_path is None:
                conv = Path(project.folder) / (p.lstrip('/') + '.tdn')
                if conv.is_file():
                    abs_path = conv
            if abs_path is not None:
                candidates.append((p, abs_path))

        if not candidates:
            return results
        results['found'] = [p for p, _ in candidates]
        self.Log(
            f"Found {len(candidates)} TDN-tagged empty COMP(s) with a "
            f"recoverable .tdn but no tracking row: "
            f"{', '.join(results['found'])}", 'WARNING')

        if not auto:
            listing = '\n'.join(f'  - {p}' for p, _ in candidates[:15])
            if len(candidates) > 15:
                listing += f'\n  ... and {len(candidates) - 15} more'
            choice = self._messageBox(
                'Embody -- Recoverable TDN COMPs',
                f'{len(candidates)} TDN-tagged COMP(s) are empty and missing '
                f'from the externalizations table, but their .tdn files still '
                f'exist on disk (the table may have been lost in a crash):'
                f'\n\n{listing}\n\n'
                f'Restore their contents from the .tdn files and re-track '
                f'them?',
                buttons=['Restore All', 'Skip'])
            if choice != 0:
                if choice == -1:
                    self.Log('Orphan-shell recovery deferred (dialogs '
                             'suppressed); will re-offer next open', 'INFO')
                return results

        # Parent-before-child so nested shells restore in order.
        candidates.sort(key=lambda c: c[0].count('/'))
        for comp_path, abs_path in candidates:
            try:
                tdn_doc = self.my.ext.TDN.tdn_load(
                    abs_path.read_text(encoding='utf-8'))
                # A parent restore's Phase 8.6 may have just filled this
                # candidate from this very file -- skip the redundant
                # destroy-and-rebuild but STILL run the tracking
                # bookkeeping below (the orphan's whole problem is its
                # missing row; review finding).
                existing = op(comp_path)
                if existing is not None and any(
                        c.type != 'annotate' for c in existing.children):
                    self.Log(f'Orphan-shell restore: {comp_path} already '
                             f'filled by its parent restore; skipping '
                             f'the redundant re-import', 'DEBUG')
                    res = {'success': True}
                else:
                    res = self.my.ext.TDN.ImportNetwork(
                        target_path=comp_path, tdn=tdn_doc,
                        clear_first=True, restore_file_links=True)
                if res.get('success'):
                    # Tag is present (that's how we found it), so the row
                    # append passes _trackTDNExport's enrollment gate.
                    self.my.ext.TDN._trackTDNExport(
                        comp_path, str(abs_path),
                        build_num=tdn_doc.get('build'),
                        touch_build=tdn_doc.get('td_build'))
                    shell = op(comp_path)
                    if shell:
                        self._reconstructAboutPage(shell, comp_path)
                        self.param_tracker.updateParamStore(shell)
                        self._storeTDNFingerprint(shell)
                    results['restored'].append(comp_path)
                else:
                    results['failed'].append(comp_path)
                    self.Log(f'Orphan-shell restore failed for {comp_path}: '
                             f'{res.get("error")}', 'ERROR')
            except Exception as e:
                results['failed'].append(comp_path)
                self.Log(f'Orphan-shell restore error for {comp_path}: {e}',
                         'ERROR')
        if results['restored']:
            self.Log(f"Orphan-shell recovery restored "
                     f"{len(results['restored'])} COMP(s) and re-tracked them",
                     'SUCCESS')
        return results

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

    def _convoyWakeState(self) -> dict:
        """Process-only Perform override state, stable across extension reinit.

        COMP storage can be baked into a .toe/TDN and an instance attribute is
        lost on syncfile reinit. A sys registry has the exact lifetime this
        capability needs: one TouchDesigner process, never a project file.
        """
        registry = getattr(sys, '_embody_convoy_wake_states', None)
        if not isinstance(registry, dict):
            registry = {}
            sys._embody_convoy_wake_states = registry
        return registry.setdefault(self.my.path, {'active': False})

    @property
    def _performModeRequested(self) -> bool:
        """The user's live Perform Mode toggle, independent of Convoy."""
        par = getattr(self.my.par, 'Performmode', None)
        return bool(par.eval()) if par is not None else False

    @property
    def _convoyWakeActive(self) -> bool:
        """True only for a process-local, main-thread-applied wake override."""
        try:
            return bool(self._convoyWakeState().get('active'))
        except Exception:
            return False

    @property
    def _performMode(self) -> bool:
        """The user's Perform request and authority for unrelated features.

        A Convoy wake must not turn autosave, externalization, visualization,
        shortcuts, viewers, or any other background subsystem back on.  Envoy
        has its own narrower suspension property below; every existing Embody
        guard continues to see the requested Perform state unchanged.
        """
        return self._performModeRequested

    @property
    def _envoyPerformMode(self) -> bool:
        """Whether Perform Mode should currently suspend Envoy itself."""
        return self._performModeRequested and not self._convoyWakeActive

    def _enterPerformMode(self) -> None:
        """Suspend all Embody features for live performance."""
        self._convoyWakeState()['active'] = False
        # Snapshot state so we can restore on exit
        state = self.my.fetch('_perform_state', None, search=False)
        if not isinstance(state, dict):
            state = {
                'envoy_was_running': bool(self.my.fetch(
                    'envoy_running', False, search=False)),
                'kb_active': self.my.op('keyboardin1').par.active.eval(),
                'exit_tagger_active': self.my.op(
                    'chopexec_exit_tagger').par.active.eval(),
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
        for p in ('Envoyenable', 'Envoyport', 'Aiclient', 'Launchaiclient', 'Aiprojectroot', 'Aiprojectrootcustom'):
            par = getattr(self.my.par, p, None)
            if par is not None:
                par.enable = False

        self.Log('Perform Mode ON -- features suspended', 'INFO')

    def _exitPerformMode(self) -> None:
        """Restore all Embody features after live performance."""
        self._convoyWakeState()['active'] = False
        state = self.my.fetch('_perform_state', {}, search=False)

        # Re-enable keyboard shortcuts and exit tagger
        self.my.op('keyboardin1').par.active = state.get('kb_active', True)
        self.my.op('chopexec_exit_tagger').par.active = state.get('exit_tagger_active', True)

        # Restore Envoy parameter enable state
        for p in ('Envoyenable', 'Envoyport', 'Aiclient', 'Launchaiclient', 'Aiprojectroot', 'Aiprojectrootcustom'):
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

    def _beginConvoyWake(self) -> bool:
        """Temporarily resume only Envoy without changing the Perform Par.

        MAIN THREAD ONLY. The loopback listener merely queues a command;
        ConvoyExt calls this method from its main-thread poll. All unrelated
        Embody guards continue to observe ``_performMode == True`` while the
        narrower ``_envoyPerformMode`` gate allows command service startup.
        """
        if not self._performModeRequested:
            return False
        state = self._convoyWakeState()
        if state.get('active'):
            return True
        state['active'] = True
        should_start = bool(self.my.fetch(
            'envoy_running', False, search=False))
        try:
            should_start = should_start or bool(self.my.par.Envoyenable.eval())
        except Exception:
            pass
        if should_start:
            run("parent.Embody.ext.Envoy.Start()", delayFrames=1)
            self.my.par.Envoystatus = 'Convoy wake starting...'
        self.Log('Convoy command service temporarily awake in Perform Mode',
                 'INFO')
        return True

    def _endConvoyWake(self) -> bool:
        """End a temporary wake and restore suspension if the Par is still On."""
        state = self._convoyWakeState()
        was_active = bool(state.get('active'))
        state['active'] = False
        if self._performModeRequested:
            self.my.ext.Envoy.Stop()
            self.my.par.Envoystatus = 'Perform Mode'
        return was_active

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
        # Annotation artifacts are collapsed to ONE warning per annotation
        # instead of one per row. A pre-guard project carries a row for the
        # annotate AND for each of its widget internals (16 rows for a
        # single annotation in the v6.0.157 field report), and this
        # enumerator runs several times per save.
        annotate_rows = {}
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
                # Legacy rows AT an annotation or inside its widget are
                # inert: the annotate tagging guards refuse to create them
                # now, but rows from older builds must neither reconstruct
                # (guts the widget's stock internals) nor re-export
                # (recreates orphan files). Reuse the comp resolved above;
                # fall back to the utility-aware path walk only when the op
                # is not live.
                if comp is not None:
                    is_annotate_row = (comp.type == 'annotate'
                                       or self._isInsideAnnotate(comp))
                else:
                    is_annotate_row = self._isAnnotateInteriorPath(comp_path)
                if is_annotate_row:
                    # Fall back to the row's PARENT, not the row path
                    # itself: an unresolvable root (the strip/restore
                    # window makes paths temporarily untestifiable) would
                    # otherwise give every row its own key and re-fan the
                    # per-row flood this aggregation exists to stop.
                    root = (self._annotateRootForPath(comp_path)
                            or comp_path.rsplit('/', 1)[0] or comp_path)
                    annotate_rows.setdefault(root, []).append(comp_path)
                    continue
                result.append((
                    comp_path,
                    self._cellVal(i, 'rel_file_path'),
                ))
        self._warnAnnotateArtifacts(annotate_rows)
        # Sort by path depth (fewest segments first) so parents are
        # imported before their children during reconstruction. Each
        # child's own .tdn file then overwrites the parent's snapshot.
        result.sort(key=lambda x: x[0].count('/'))
        return result

    def _warnAnnotateArtifacts(self, annotate_rows: dict) -> None:
        """Emit ONE warning per annotation holding legacy rows.

        Previously this warned once per ROW per session: a single
        annotation carries a row for itself plus one per widget internal
        (16 in the v6.0.157 field report), and the dedup set lives on the
        extension instance, so an extension reinit re-armed all of them --
        16 near-identical lines per save, drowning the log.

        Deduped on the annotation path ALONE, never on the row count: the
        count legitimately oscillates across a save (the strip/restore
        window leaves some paths temporarily unresolvable), and a
        count-keyed dedup would re-arm on every oscillation.
        """
        if not annotate_rows:
            return
        # Instance attribute, deliberately NOT COMP storage. Storage IS
        # persisted into the .tdn/.toe export, so keeping the set there
        # (a) baked whatever paths happened to be warned -- including
        # test-sandbox paths -- into a committed file, and (b) silenced
        # the warning FOREVER across sessions, defeating its purpose.
        # An extension reinit therefore re-arms this, which is acceptable:
        # the per-annotation aggregation above already turns what was 16
        # lines per sweep into one.
        warned = getattr(self, '_annotate_interior_warned', None)
        if warned is None:
            warned = set()
            self._annotate_interior_warned = warned
        for root, paths in sorted(annotate_rows.items()):
            if root in warned:
                continue
            warned.add(root)
            self.Log(
                f"Skipping {len(paths)} legacy externalization row(s) for "
                f"annotation '{root}' -- inert artifacts from before the "
                f"annotate guards. To clear them, remove those rows in the "
                f"Embody manager; this keeps the annotation itself. "
                f"(Deleting the annotation also clears them, but destroys "
                f"the documentation it holds.)",
                'WARNING')

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
                    # cover them), inside an excluded COMP (app-managed,
                    # invisible to TDN), inside ANY annotateCOMP (widget
                    # internals are TD-managed stock content -- flagging the
                    # color/i/help tables of a code-created annotation as
                    # "at risk" is what externalized them as bogus per-DAT
                    # files), or inside a palette clone -- a clone's
                    # internal DATs are regenerable palette boilerplate (e.g. an
                    # annotateCOMP's button help tables), never user content, so
                    # they must never trip the content-safety warning.
                    if (parent_op.path in tdn_paths
                            or parent_op.type == 'annotate'
                            or self.my.ext.TDN._hasExcludeTag(parent_op)
                            or self.my.ext.TDN._isPaletteClone(parent_op)):
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
    # Embody-only runtime keys, ON TOP OF TDNExt.SKIP_STORAGE_KEYS. These
    # are never surfaced as at-risk storage but are not part of the
    # serialization contract, so they live here rather than in TDNExt.
    #
    # This used to be a second hand-maintained literal documented as a
    # "superset of TDNExt.SKIP_STORAGE_KEYS". It was not: the two drifted
    # in BOTH directions (this one omitted git_status, _tdn_fingerprints
    # and _suppress_dialogs; TDNExt's omitted eleven keys Embody itself
    # classifies as runtime, so those serialized into committed .tdn
    # files). _storageSkipKeys() now derives the union at call time, so
    # adding a key to TDNExt is enough and the pair cannot drift again.
    _STORAGE_SKIP_EXTRA = {
        '_tdn_external_wires', '_tdn_pane_restore',
        '_tdn_palette_handling', '_smoke_test_responses',
        '_tdn_mode_migration_shown', '_tdn_migration_scheduled',
        '_tdn_migration_prev_enable',
    }

    def _storageSkipKeys(self) -> set:
        """Runtime storage keys never surfaced as at-risk.

        Single source of truth is TDNExt.SKIP_STORAGE_KEYS (what never
        serializes); this adds Embody-only runtime keys. Falls back to the
        extras alone if TDNExt cannot be reached, which only makes the
        at-risk prompt noisier -- never less safe.
        """
        base = set()
        try:
            base = set(self.my.op('TDNExt').module.SKIP_STORAGE_KEYS)
        except Exception:
            pass
        return base | self._STORAGE_SKIP_EXTRA

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
                # Skip palette clones -- their storage is palette-managed
                # boilerplate (e.g. an annotateCOMP's AnnotateExtStored),
                # never user content.
                if self.my.ext.TDN._isPaletteClone(target):
                    continue
                # Skip ops inside a nested TDN COMP, an excluded COMP, or a
                # palette clone (regenerable palette internals -- not authored).
                if target is not comp:
                    inside_nested = False
                    parent_op = target.parent()
                    while parent_op and parent_op.path != comp_path:
                        # Mirror of _findAtRiskDATs' walk: annotate widget
                        # internals are TD-managed, never user storage.
                        if (parent_op.path in tdn_paths
                                or parent_op.type == 'annotate'
                                or self.my.ext.TDN._hasExcludeTag(parent_op)
                                or self.my.ext.TDN._isPaletteClone(parent_op)):
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

                skip_keys = self._storageSkipKeys()
                risky_keys = [
                    k for k in storage.keys()
                    if k not in self._STORAGE_CONTROL_KEYS
                    and k not in skip_keys
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
                # Resolve the tag from the DAT's CONTENT type, not a bare
                # type->tag map. _inferDATTagValue reads a text DAT's
                # language/extension, so a GLSL shader (type 'text',
                # language 'glsl') externalizes as .glsl -- the old
                # dat_type_to_tag['text']='Pytag' wrongly wrote shaders as .py.
                tag_value = self._inferDATTagValue(dat)
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
                # Python tracebacks sit on a SEPARATE surface. This method's
                # docstring has always promised "scripts", but errors()
                # cannot see them -- the same blind spot get_op_errors had
                # until 2026-08-22. Kept whole (not split per line) so one
                # traceback counts as one error in the startup report.
                script_str = child.scriptErrors()
                if script_str:
                    errors.append(f'{child.path}: {script_str.strip()}')
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
        failed = 0

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

            # Per-row guard: one bad row (wrong family for its strategy,
            # missing par, unreadable file) must not abort the whole pass.
            try:
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
                # Par writes come FIRST in each branch: a wrongly-typed row
                # (e.g. a DAT sitting in a tox row) then fails before the
                # tag mutation, so broken rows don't accumulate stray tags.
                if strategy not in ('tox', 'tdn'):
                    # DAT reconciliation
                    oper.par.file.readOnly = False
                    oper.par.file = rel_file_path
                    oper.par.syncfile = True
                    oper.par.file.readOnly = True
                    oper.tags.add(tag)
                    self._setDATLanguageForTag(oper, tag)

                elif strategy == 'tox':
                    # TOX COMP reconciliation. enableexternaltoxpulse is
                    # the load trigger -- reloadtoxpulse does not exist on
                    # TD 2025 COMPs. A missing file must fail the row
                    # loud: pulsing against it is a silent no-op (no
                    # exception, no scriptError), which would count the
                    # row reconciled while nothing reloaded.
                    if not self.buildAbsolutePath(rel_file_path).is_file():
                        raise FileNotFoundError(
                            f'.tox missing on disk: {rel_file_path}')
                    # The tag goes on AFTER the pulse: the reload replaces
                    # the COMP from disk, and a tag added before it does
                    # not survive (same reasoning as RestoreTOXComps'
                    # post-load re-tag).
                    oper.par.externaltox.readOnly = False
                    oper.par.externaltox = rel_file_path
                    oper.par.externaltox.readOnly = True
                    oper.par.enableexternaltox = True
                    oper.par.enableexternaltoxpulse.pulse()
                    oper.tags.add(tag)
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

            except Exception as e:
                failed += 1
                self.Log(f"Failed to reconcile '{path}' ({strategy}): {e}",
                         "ERROR")

        if failed:
            self.Log(f"Reconciled {reconciled} operator(s); {failed} row(s) "
                     f"failed", "WARNING")
        elif reconciled:
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

            # Configure and load the .tox. Setting externaltox +
            # enableexternaltox alone does NOT load mid-session on TD 2025
            # (auto-load only happens at .toe open) --
            # enableexternaltoxpulse is the explicit, synchronous trigger.
            try:
                new_comp.par.externaltox = self.normalizePath(rel_tox_path)
                new_comp.par.externaltox.readOnly = True
                new_comp.par.enableexternaltox = True
                new_comp.par.enableexternaltoxpulse.pulse()

                # Handle timing issue (same workaround as
                # _setupCompForExternalization). Everything else about
                # this row -- tag, color, position, the success/failure
                # verdict -- must WAIT for the deferred verify: the load
                # has not landed yet, and a tag applied now would be
                # wiped when it does.
                timing_error = ("Cannot load external tox from path"
                                in new_comp.scriptErrors())
                if timing_error:
                    new_comp.allowCooking = False
                    run(lambda p=new_comp.path: self._safeAllowCooking(p, True),
                        delayFrames=1)
                    run(lambda p=new_comp.path, r=rel_tox_path:
                        self._verifyTOXRestoreLoaded(p, r), delayFrames=3)
                    self.Log(f'Restore of {comp_path} deferred one frame '
                             f'(cook-timing) -- verifying shortly', 'INFO')
                    continue

                # The pulse loads synchronously and externalTimeStamp is
                # the success signal: it stays 0 when the .tox did not
                # load (a valid-but-empty .tox still stamps it, and a
                # failed load posts NO script error at all). Destroy the
                # dead shell and fail loud: a leftover shell blocks the
                # next startup restore (the op exists, so it gets skipped)
                # and a later Save() would export an empty .tox over the
                # good file.
                if new_comp.externalTimeStamp == 0:
                    self.Log(f'Restore of {comp_path} produced an empty '
                             f'COMP -- .tox did not load: {rel_tox_path}',
                             'ERROR')
                    new_comp.destroy()
                    errors += 1
                    continue

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
                # Never leave a dead shell behind: it blocks the next
                # startup restore and a later Save() would export it
                # empty over the good .tox.
                try:
                    if new_comp.valid and new_comp.externalTimeStamp == 0:
                        new_comp.destroy()
                except Exception:
                    pass

        self._logTOXRestorationReport(len(to_restore), restored, errors)

    def _verifyTOXRestoreLoaded(self, comp_path: str,
                                rel_tox_path: str) -> None:
        """Deferred completion of a cook-timing-deferred TOX restore.

        externalTimeStamp stays 0 when the .tox never loaded (a failed
        load posts no script error, so it is the only signal; a
        valid-but-empty .tox still stamps it). On success this finishes
        the restore -- the load wipes tags, so metadata must be applied
        HERE, not before the load. On failure it destroys the dead shell
        so the next startup can retry and no Save() can export it empty.
        """
        oper = op(comp_path)
        if oper is None or not oper.valid:
            return
        if oper.externalTimeStamp != 0:
            tox_tag = self.my.par.Toxtag.val
            if tox_tag and tox_tag not in oper.tags:
                oper.tags.add(tox_tag)
            oper.color = (self.my.par.Toxtagcolorr.eval(),
                          self.my.par.Toxtagcolorg.eval(),
                          self.my.par.Toxtagcolorb.eval())
            self._restorePositionFromTable(oper, comp_path)
            self.Log(f'Restored {comp_path} from {rel_tox_path} '
                     f'(deferred)', 'SUCCESS')
            return
        self.Log(f'Restore of {comp_path} produced an empty COMP -- .tox '
                 f'did not load: {rel_tox_path}', 'ERROR')
        try:
            oper.destroy()
        except Exception:
            pass

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
        empty), so it is meaningless -- the fingerprint-derived runtime
        DirtyState maintained by dirtyHandler is authoritative. Using
        oper.dirty here counted every clean TDN COMP as dirty.

        For DATs and missing operators, uses the runtime DirtyState.
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
            val = self.DirtyState(op_path)
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
        """Reset operator to Embody's default node color.

        Annotations are exempt. Making the removal primitives utility-aware
        newly exposed annotateCOMPs to this call (bare op() used to return
        None, so it never ran), and an annotate's stock colour is not
        Embody's grey -- restyling it would contradict the cleanup remedy's
        own promise that the annotation itself is kept untouched."""
        if oper is not None and oper.family == 'COMP' \
                and oper.type == 'annotate':
            return
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
                # no-console-window-exempt: explorer.exe is a GUI app --
                # Windows never allocates a console for it (see
                # test_no_console_window).
                subprocess.Popen(['explorer', f'/select,{filepath}'])
        except Exception as e:
            self.Log(f'Failed to open file location: {e}', 'ERROR')

    def LaunchAIClient(self) -> None:
        """Open the AI client selected in the Aiclient menu at the project root.

        Editors (Cursor, Windsurf; Copilot -> VS Code) open the root as a
        workspace; CLI tools (Claude, Codex, Gemini) open in a new terminal at
        the root. Fire-and-forget button callback -- see embody_launch.
        """
        return mod.embody_launch.launch_ai_client(self)

    def _resolveCliAbs(self, cli: str) -> Optional[str]:
        """Absolute path to a CLI via fast filesystem probes, or None -- see embody_launch."""
        return mod.embody_launch.resolve_cli_abs(self, cli)

    def _launchEnv(self) -> dict:
        """Process environment with TouchDesigner's injected vars stripped -- see embody_launch."""
        return mod.embody_launch.launch_env(self)

    def _launchEditor(self, cwd, app_name, bundle_id=None, win_exe_candidates=(),
                      win_shim=None, mac_cli=None, mac_alt_names=(), install=None) -> bool:
        """Open a GUI editor with cwd as its workspace -- see embody_launch."""
        return mod.embody_launch.launch_editor(
            self, cwd, app_name, bundle_id=bundle_id,
            win_exe_candidates=win_exe_candidates, win_shim=win_shim,
            mac_cli=mac_cli, mac_alt_names=mac_alt_names, install=install)

    def _buildTerminalScript(self, cwd, cli, abs_cli, install=None) -> str:
        """macOS .command script text that cd's to cwd and runs <cli> -- see embody_launch."""
        return mod.embody_launch.build_terminal_script(self, cwd, cli, abs_cli, install)

    def _buildTerminalScriptWin(self, cwd, cli, abs_cli, install=None) -> str:
        """Windows twin of _buildTerminalScript: the .bat run via cmd /K -- see embody_launch."""
        return mod.embody_launch.build_terminal_script_win(self, cwd, cli, abs_cli, install)

    def _launchTerminal(self, cwd, cli, install=None) -> bool:
        """Open a new terminal at cwd running <cli> -- see embody_launch."""
        return mod.embody_launch.launch_terminal(self, cwd, cli, install)

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
                    choice = self._messageBox('Import TDN',
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
            choice = self._messageBox('Import TDN',
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

        # No file logging until the project is saved: the relative folder
        # resolves against TD's default location on a never-saved project
        # and the wizard's save step then orphans it. Ring buffer, FIFO
        # and textport logging are upstream and unaffected; the first Log
        # call after the save resumes file logging in the real folder.
        if not self._projectSavedOnDisk():
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
