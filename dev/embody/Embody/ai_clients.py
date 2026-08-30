"""AI client registry -- one row per client Embody can configure and launch.

Single source of truth for every client-specific fact: how to launch it,
which MCP config file it reads, which rules/skills directories it
discovers, the paths that prove its config is present, and what Uninstall
must remove. These facts previously lived in eight places that drifted
independently (EmbodyExt._AICLIENT_LAUNCH / _AI_CONFIG_FILES, the if/elif
in embody_git.extract_ai_config, client_files_missing, two Uninstall
lists, the Aiclient menu, the wizard) -- 'vscode' and 'codex' never had a
restore-on-open probe at all, so their config could not regenerate.

Adding a client = one CLIENTS row (+ its Configure-For toggle).

Pure data and pure helpers: no TD objects touched at module level, so
this imports safely during extension init. The writers that consume the
registry live in embody_git (instruction files) and envoy_setup (MCP
config files).
"""

from pathlib import Path


# ==========================================================================
# LAUNCH SPECS
# ==========================================================================
# How the Launchaiclient button opens a client at the project root
# (_findProjectRoot(), which honors Aiprojectroot).
#   kind 'editor'   -> GUI editor opened with the root as its workspace
#   kind 'terminal' -> new login-shell terminal at the root running the CLI
# Editors resolve the REAL app/exe, never a PATH shim -- a `code` shim can be
# hijacked (e.g. Cursor installs its own). CLIs run inside a real terminal so
# its login shell rebuilds PATH (defeats the Dock-truncated-PATH problem where
# a CLI in ~/.local/bin is invisible to a Dock-launched TD). A client with
# 'launch': None (e.g. copilot, none) -> Launchaiclient logs "no launcher".

VSCODE_LAUNCH = {
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


# ==========================================================================
# CLIENT REGISTRY
# ==========================================================================
# Row schema -- every key is optional except 'label':
#
#   label         Human name shown in the UI and logs.
#   launch        Launch spec (above) or None for a client with no
#                 launcher of its own (Copilot runs inside VS Code).
#   launch_alias  Token whose launcher this client shares, for the UI to
#                 explain WHY it has no launcher of its own.
#   mcp           Which MCP config file this client reads:
#                   path   repo-relative (scope 'project') or
#                          home-relative (scope 'user') path
#                   key    root key holding the server map -- VS Code uses
#                          'servers', everyone else 'mcpServers'
#                   style  server-entry shape, see envoy_setup
#                   scope  'project' (in the repo) or 'user' (in $HOME --
#                          shared across ALL the user's projects, so it
#                          is always confirmed, never silently written)
#                   owner  another client's token when that client's
#                          writer already produces this file
#   rules         Where per-rule instruction files go:
#                   dir/ext/style -- style drives frontmatter handling
#   skills        Where SKILL.md folders go (dir), or None if the client
#                 has no skills mechanism.
#   docs          Extra generated top-level docs (CLAUDE.md, GEMINI.md).
#   writer        Bespoke writer key in embody_git for the parts a table
#                 cannot express; None means the generic writer suffices.
#   probe         Restore-on-open check: a list of GROUPS. The client
#                 counts as configured only when every group has at least
#                 one existing path. An empty list means "nothing
#                 client-specific to restore" (AGENTS.md is checked for
#                 every client separately).
#   cleanup_dirs  Directories Uninstall marker-sweeps then rmdirs.
#   cleanup_files Single files Uninstall marker-sweeps.
#
# AGENTS.md is written for EVERY client and so appears in no row.

CLIENTS = {
    'claudecode': {
        'label': 'Claude Code',
        'launch': {'kind': 'terminal', 'cli': 'claude', 'install': {
            'name': 'Claude Code',
            'mac': 'curl -fsSL https://claude.ai/install.sh | bash',
            'mac_alt': 'brew install --cask claude-code',
            'win': 'curl -fsSL https://claude.ai/install.cmd -o install.cmd '
                   '&& install.cmd && del install.cmd',
            'win_alt': 'winget install Anthropic.ClaudeCode',
            'docs': 'https://code.claude.com/docs/en/setup',
        }},
        # .mcp.json is Claude Code's format AND Envoy's baseline:
        # configure_mcp_client writes it on every deploy regardless of
        # which clients are selected, so the generic writer skips it.
        'mcp': {'path': '.mcp.json', 'key': 'mcpServers',
                'style': 'stdio_typed', 'scope': 'project',
                'owner': 'baseline'},
        'rules': {'dir': '.claude/rules', 'ext': '.md', 'style': 'strip'},
        'skills': {'dir': '.claude/skills'},
        'docs': ['CLAUDE.md (or ENVOY.md)'],
        'writer': 'claudecode',
        'probe': [['CLAUDE.md', 'ENVOY.md'], ['.claude/rules']],
        'cleanup_dirs': ['.claude/rules', '.claude/skills', '.claude'],
        'cleanup_files': ['CLAUDE.md', 'ENVOY.md'],
    },

    'opencode': {
        'label': 'OpenCode',
        'launch': {'kind': 'terminal', 'cli': 'opencode', 'install': {
            'name': 'OpenCode',
            'mac': 'curl -fsSL https://opencode.ai/install | bash',
            'mac_alt': 'brew install anomalyco/tap/opencode',
            'win': 'choco install opencode',
            'win_alt': 'npm install -g opencode-ai',
            'win_alt_note': 'needs Node.js -- https://nodejs.org',
            'docs': 'https://opencode.ai/docs/',
        }},
        # Bespoke writer: opencode.json also carries an instructions glob
        # and a permission posture block, neither of which is an MCP key.
        'mcp': {'path': 'opencode.json', 'key': 'mcp', 'style': 'opencode',
                'scope': 'project', 'owner': 'opencode'},
        # Shares Claude Code's directories on purpose: OpenCode's
        # Claude-compat layer discovers .claude/skills/ natively and the
        # generated opencode.json loads .claude/rules/ via an
        # instructions glob -- one copy on disk, no drift.
        'rules': {'dir': '.claude/rules', 'ext': '.md', 'style': 'strip'},
        'skills': {'dir': '.claude/skills'},
        'docs': [],
        'writer': 'opencode',
        'probe': [['opencode.json'], ['.claude/rules']],
        'cleanup_dirs': ['.claude/rules', '.claude/skills', '.claude'],
        'cleanup_files': [],
    },

    'codex': {
        'label': 'Codex',
        'launch': {'kind': 'terminal', 'cli': 'codex', 'install': {
            'name': 'Codex CLI',
            'mac': 'curl -fsSL https://chatgpt.com/codex/install.sh | sh',
            'mac_alt': 'brew install --cask codex',
            'win': 'powershell -ExecutionPolicy ByPass -c '
                   '"irm https://chatgpt.com/codex/install.ps1 | iex"',
            'win_alt': 'npm install -g @openai/codex',
            'win_alt_note': 'needs Node.js -- https://nodejs.org',
            'docs': 'https://developers.openai.com/codex/cli',
        }},
        # Codex reads three levels of config.toml (system, user, then
        # project). The project-level one is the right home for a server
        # that only applies to this codebase -- but Codex IGNORES it
        # until the project is trusted, so the write logs that step.
        'mcp': {'path': '.codex/config.toml', 'key': 'mcp_servers',
                'style': 'toml', 'scope': 'project'},
        # Codex reads AGENTS.md natively; no per-rule dialect of its own.
        'rules': None,
        # Codex discovers SKILL.md folders under .agents/skills (cwd, then
        # parents up to the repo root, then ~/.agents/skills) -- the same
        # agentskills.io contract Claude Code uses, name + description
        # required. Shared with Antigravity, Cursor and Gemini; the writer
        # is idempotent so a second client re-stamps identical files.
        'skills': {'dir': '.agents/skills'},
        'docs': [],
        'writer': None,
        'probe': [['.codex/config.toml']],
        'cleanup_dirs': ['.codex', '.agents/skills', '.agents'],
        'cleanup_files': [],
    },

    'gemini': {
        'label': 'Gemini',
        'launch': {'kind': 'terminal', 'cli': 'gemini', 'install': {
            'name': 'Gemini CLI',
            'mac': 'npm install -g @google/gemini-cli',
            'mac_alt': 'brew install gemini-cli',
            'win': 'npm install -g @google/gemini-cli',
            'note': 'needs Node.js 20 or newer -- https://nodejs.org',
            'docs': 'https://www.geminicli.com/docs/get-started/installation',
        }},
        'mcp': {'path': '.gemini/settings.json', 'key': 'mcpServers',
                'style': 'command_args', 'scope': 'project'},
        'rules': None,
        # Gemini CLI reads workspace skills from .gemini/skills or
        # .agents/skills (the .agents alias wins within a tier); the
        # shared folder means one copy on disk serves every client.
        'skills': {'dir': '.agents/skills'},
        # GEMINI.md is a thin @import of AGENTS.md -- Gemini does not read
        # AGENTS.md itself.
        'docs': ['GEMINI.md'],
        'writer': 'gemini',
        'probe': [['GEMINI.md'], ['.gemini/settings.json']],
        'cleanup_dirs': ['.gemini', '.agents/skills', '.agents'],
        'cleanup_files': ['GEMINI.md'],
    },

    'vscode': {
        'label': 'VS Code',
        'launch': VSCODE_LAUNCH,
        # VS Code is the one client whose root key is 'servers', not
        # 'mcpServers' -- configs are NOT interchangeable with Cursor's.
        'mcp': {'path': '.vscode/mcp.json', 'key': 'servers',
                'style': 'stdio_typed', 'scope': 'project'},
        'rules': None,
        'skills': None,
        'docs': [],
        'writer': None,
        'probe': [['.vscode/mcp.json']],
        'cleanup_dirs': ['.vscode'],
        'cleanup_files': [],
    },

    'copilot': {
        'label': 'GitHub Copilot',
        # Copilot is an extension INSIDE VS Code, not an app: it has no
        # launcher of its own and shares .vscode/mcp.json for MCP.
        'launch': None,
        'launch_alias': 'vscode',
        'mcp': {'path': '.vscode/mcp.json', 'key': 'servers',
                'style': 'stdio_typed', 'scope': 'project',
                'owner': 'vscode'},
        'rules': {'dir': '.github/instructions', 'ext': '.instructions.md',
                  'style': 'copilot'},
        'skills': None,
        'docs': [],
        'writer': 'copilot',
        'probe': [['.github/copilot-instructions.md']],
        'cleanup_dirs': ['.github/instructions', '.github'],
        'cleanup_files': ['.github/copilot-instructions.md'],
    },

    'cursor': {
        'label': 'Cursor',
        'launch': {
            'kind': 'editor', 'app': 'Cursor',
            'bundle': 'com.todesktop.230313mzl4w4u92',
            'mac_cli': '/Applications/Cursor.app/Contents/Resources/app/bin/cursor',
            'win_exe': [r'%LOCALAPPDATA%\Programs\cursor\Cursor.exe'],
            'win_shim': 'cursor',
            'install': 'https://cursor.com/download  (macOS: brew install --cask cursor)',
        },
        'mcp': {'path': '.cursor/mcp.json', 'key': 'mcpServers',
                'style': 'command_args', 'scope': 'project'},
        'rules': {'dir': '.cursor/rules', 'ext': '.mdc', 'style': 'cursor'},
        # Cursor loads .agents/skills, .cursor/skills and (legacy)
        # .claude/skills; it honors disable-model-invocation and requires
        # name + description like everyone else.
        'skills': {'dir': '.agents/skills'},
        'docs': [],
        'writer': None,
        'probe': [['.cursor/rules'], ['.cursor/mcp.json']],
        'cleanup_dirs': ['.cursor/rules', '.cursor', '.agents/skills', '.agents'],
        'cleanup_files': [],
    },

    'windsurf': {
        'label': 'Windsurf',
        'launch': {
            'kind': 'editor', 'app': 'Windsurf',
            'bundle': 'com.exafunction.windsurf',
            'alt_names': ('Devin Desktop',),   # Windsurf rebrand
            'win_exe': [r'%LOCALAPPDATA%\Programs\Windsurf\Windsurf.exe'],
            'win_shim': 'windsurf',
            'install': 'https://windsurf.com/editor/download  (macOS: brew install --cask windsurf)',
        },
        # Windsurf has NO project-level MCP config -- Cascade reads only
        # ~/.codeium/windsurf/mcp_config.json, which is shared by every
        # project the user opens. Embody will not write a global file
        # that points all of them at THIS project's bridge, so this one
        # is documented for a one-time manual paste, never auto-written
        # (owner 'manual'). Flip to a real writer only if Windsurf gains
        # workspace-scoped config.
        'mcp': {'path': '.codeium/windsurf/mcp_config.json',
                'key': 'mcpServers', 'style': 'command_args',
                'scope': 'user', 'owner': 'manual'},
        'rules': {'dir': '.windsurf/rules', 'ext': '.md', 'style': 'raw'},
        'skills': None,
        'docs': [],
        'writer': None,
        'probe': [['.windsurf/rules']],
        'cleanup_dirs': ['.windsurf/rules', '.windsurf'],
        'cleanup_files': [],
    },

    'antigravity': {
        'label': 'Antigravity',
        # A VS Code fork, so the Electron launch path and the
        # ELECTRON_RUN_AS_NODE strip apply unchanged. Every value below is
        # read off a real 2.5.5 install (winget Google.AntigravityIDE) and
        # its product.json -- nameLong 'Antigravity IDE',
        # applicationName 'antigravity-ide', darwinBundleIdentifier
        # 'com.google.antigravity-ide'. Note the SPACES: the folder and
        # the exe are both 'Antigravity IDE', and it is a per-user
        # install, not Program Files. Google also ships a separate
        # standalone 'Antigravity' agent manager and an 'Antigravity CLI';
        # this row is the IDE, which is what opens a workspace folder.
        'launch': {
            'kind': 'editor', 'app': 'Antigravity IDE',
            'bundle': 'com.google.antigravity-ide',
            'mac_cli': '/Applications/Antigravity IDE.app/Contents/Resources/app/bin/antigravity-ide',
            'alt_names': ('Antigravity',),
            'win_exe': [
                r'%LOCALAPPDATA%\Programs\Antigravity IDE\Antigravity IDE.exe',
                r'%ProgramFiles%\Antigravity IDE\Antigravity IDE.exe',
            ],
            'win_shim': 'antigravity-ide',
            'install': 'https://antigravity.google/download  '
                       '(Windows: winget install Google.AntigravityIDE)',
        },
        # Workspace-scoped, same stdio bridge as everyone else. The file
        # is JSONC (the IDE registers it as such and validates it against
        # its own schema, whose entries are command/args/env/cwd with
        # additionalProperties:false -- so NO 'type' key, unlike VS Code).
        # The global counterpart is ~/.gemini/config/mcp_config.json.
        'mcp': {'path': '.agents/mcp_config.json', 'key': 'mcpServers',
                'style': 'command_args', 'scope': 'project'},
        # '.agents/rules' and '.agents/skills' are both literal strings in
        # the shipped language server binary; the docs confirm .agents/rules
        # is the default with .agent/rules kept for backward compatibility.
        'rules': {'dir': '.agents/rules', 'ext': '.md', 'style': 'strip'},
        # Same SKILL.md contract as Claude Code: frontmatter with a
        # required description, name matching the directory slug.
        'skills': {'dir': '.agents/skills'},
        # Antigravity reads AGENTS.md natively (GEMINI.md is only for
        # Antigravity-specific overrides), so no doc of its own.
        'docs': [],
        'writer': None,
        'probe': [['.agents/rules'], ['.agents/mcp_config.json']],
        'cleanup_dirs': ['.agents/rules', '.agents/skills', '.agents'],
        'cleanup_files': [],
    },
}


# ==========================================================================
# DERIVED ACCESSORS
# ==========================================================================
# Everything that used to be its own hand-maintained table.

def tokens():
    """Every client token, in registry (menu) order."""
    return list(CLIENTS.keys())


def spec(token):
    """One client's row, or an empty dict for unknown/'none' tokens."""
    return CLIENTS.get(token) or {}


def label(token):
    """Human name for a token ('claudecode' -> 'Claude Code')."""
    return spec(token).get('label') or token


def launch_table():
    """Token -> launch spec, for every client that can be opened.

    Replaces EmbodyExt._AICLIENT_LAUNCH. A client with 'launch_alias'
    resolves to the SAME spec object as the client it borrows from, so
    Copilot still opens VS Code while the registry records that it owns
    no launcher of its own. A token with neither key is absent, and a
    missing key still means "no launcher" to callers.
    """
    table = {}
    for token, row in CLIENTS.items():
        spec_ = row.get('launch')
        if not spec_:
            alias = row.get('launch_alias')
            spec_ = alias and (CLIENTS.get(alias) or {}).get('launch')
        if spec_:
            table[token] = spec_
    return table


def config_files(token):
    """Human-readable footprint for the Advanced-mode consent dialog.

    Replaces EmbodyExt._AI_CONFIG_FILES. Directories keep their trailing
    slash so the dialog reads the way it always has.
    """
    row = spec(token)
    files = list(row.get('docs') or [])
    mcp = row.get('mcp') or {}
    # 'baseline' is written by the Envoy deploy itself and already
    # confirmed there; 'manual' is never written at all. Listing either
    # would tell the user we are about to touch a file we are not.
    if mcp.get('path') and mcp.get('owner') not in ('baseline', 'manual'):
        files.append(_mcp_display(mcp))
    for section in ('rules', 'skills'):
        block = row.get(section)
        if block:
            files.append(block['dir'].rstrip('/') + '/')
    if row.get('writer') == 'copilot':
        files.insert(0, '.github/copilot-instructions.md')
    return files


def _mcp_display(mcp):
    """Display path for an MCP config, marking user-scope ones as ~/."""
    if mcp.get('scope') == 'user':
        return '~/' + mcp['path']
    return mcp['path']


def mcp_targets(token):
    """MCP config specs this client needs written by the generic writer.

    Skips the ones another writer already owns: the '.mcp.json' baseline
    (written on every Envoy deploy) and opencode.json (bespoke writer).
    """
    mcp = spec(token).get('mcp') or {}
    if not mcp.get('path') or mcp.get('owner') in ('baseline', 'opencode',
                                                   'manual'):
        return []
    return [mcp]


def project_mcp_specs():
    """Every project-scope MCP config Embody may have written, deduped.

    Uninstall walks this: each file needs its 'envoy' entry stripped in
    that client's own shape. Includes the .mcp.json baseline and
    opencode.json (both written by their own writers) and excludes
    user-scope files, which Embody never touches.
    """
    specs, seen = [], set()
    for row in CLIENTS.values():
        mcp = row.get('mcp') or {}
        if (not mcp.get('path') or mcp.get('scope') != 'project'
                or mcp.get('owner') == 'manual'):
            continue
        if mcp['path'] in seen:
            continue
        seen.add(mcp['path'])
        specs.append(mcp)
    return specs


def probe_groups(token):
    """Restore-on-open probe groups for a client (see the row schema)."""
    return spec(token).get('probe') or []


def is_missing(target_dir, token):
    """True when this client's config should be (re)generated.

    Replaces embody_git.client_files_missing. A client counts as
    configured only when EVERY probe group has at least one existing
    path; a client with no groups is never "missing" on its own.
    User-scope MCP config is deliberately not probed -- it lives outside
    the project and must never be rewritten by a project opening.
    """
    root = Path(target_dir)
    for group in probe_groups(token):
        if not any((root / rel).exists() for rel in group):
            return True
    return False


def cleanup_dirs():
    """Every directory Uninstall touches, deepest-first.

    Deepest-first matters: '.claude/rules' must be emptied and rmdir'd
    before '.claude' can succeed. Deduped across clients that share a
    directory (Claude Code and OpenCode both use .claude/).
    """
    seen = []
    for row in CLIENTS.values():
        for d in row.get('cleanup_dirs') or []:
            if d not in seen:
                seen.append(d)
    return sorted(seen, key=lambda d: (-d.count('/'), d))


def cleanup_sweep_dirs():
    """Directories whose contents Uninstall marker-sweeps file by file.

    Only the LEAF directories Embody fills with generated files -- never
    a parent like '.claude', whose other contents (settings.local.json,
    the user's own files) are not marker-bearing and must not be walked.
    """
    return [d for d in cleanup_dirs() if '/' in d]


def cleanup_files():
    """Every single file Uninstall marker-sweeps, deduped."""
    seen = []
    for row in CLIENTS.values():
        for f in row.get('cleanup_files') or []:
            if f not in seen:
                seen.append(f)
    return seen
