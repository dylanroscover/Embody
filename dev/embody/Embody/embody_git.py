"""EmbodyExt AI-config generation + git status/init (module DAT).

Module DAT (mod.embody_git) called by EmbodyExt -- almost entirely on the MAIN
THREAD (the ext-diet WP9 clusters C6 + C15 + C7). Holds:

  - C6  AI-config / templates / install-manifest: extract_ai_config and the
        per-client writers (AGENTS.md, CLAUDE.md, .claude/, .cursor/, .github/,
        .windsurf/, GEMINI.md), the generated-file hash manifest + marker-gated
        write_template, and the install manifest (footprint records so Uninstall
        can reverse Embody's additions precisely).
  - C15 git status: the pure/tested parse_git_porcelain / map_changed_to_ops /
        row_has_changes, find_git_root_sync, and update_git_status (async scan).
  - C7  init_envoy / init_git / reset (promoted API).

EmbodyExt keeps a thin delegating stub for every function here (identical
signatures; promoted names stay UpperCamelCase). No module-level TD access --
each function takes the ext instance (`ext`) and reaches TD through it (ext.Log,
ext.my, ext._findProjectRoot, ...) or through the TD globals (op, project, run,
parent) available inside the bodies at main-thread call time. The former
@staticmethods (parse_git_porcelain / map_changed_to_ops / row_has_changes) stay
pure -- no ext arg.

THREAD NOTE (the WP4/_get_docs trap): update_git_status runs on the main thread,
but it hands a WORKER-thread closure a reference to parse_git_porcelain. It
captures the module-level function DIRECTLY (`parse = parse_git_porcelain`,
resolved on the main thread) so the worker calls a plain pure function and NEVER
touches `mod` (a TD object) off-main. The facade's _parseGitPorcelain stub is
for the unit tests (main thread) -- the worker must not, and does not, go
through it.

DISPATCH CONTRACT: intra-module calls are module-local EXCEPT the patchable
seams the unit tests monkeypatch on the instance -- _manifestRecordCreatedFile /
_manifestRecordAppendedFile (test_tool_permissions) and _extractAIConfig /
_upgradeEnvoy (test_setup_wizard / test_envoy_lifecycle_hardening) -- which route
via ext.* so the patches take effect. Calls to spine methods that stay on the
facade (_findProjectRoot, _guardFileWrite, createExternalizationsTable, the
Envoy sub-config methods, InitEnvoy) and to facade class attributes
(_TEMPLATE_MAP_RULES/_SKILLS, _AI_CONFIG_FILES, _HASH_MANIFEST, _INSTALL_MANIFEST,
_EMBODY_MARKER, _consent_bulk, _startup_config_pass) also go through ext.* -- the
constants stay class attrs on EmbodyExt (the /release skill's sync contract and
the unit tests both read them off the ext).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


# TD is a GUI process on Windows: it owns no console, so spawning a console
# program (git.exe) makes Windows allocate a NEW console window -- a visible
# flash over the user's TD. CREATE_NO_WINDOW suppresses it. Absent off-Windows,
# hence getattr. EVERY subprocess spawned from inside TD needs this.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ==========================================================================
# AI CLIENT CONFIG GENERATION (C6)
# ==========================================================================

def extract_ai_config(ext):
    """Write the AI assistant config for every selected client.

    Dispatch is registry-driven (mod.ai_clients): a client contributes
    rules, skills, docs, and at most one bespoke step, so adding a client
    needs no branch here. 'none' selects nothing but still gets AGENTS.md.
    """
    target_dir = ext._findProjectRoot()
    clients = selected_clients(ext)

    def _write():
        # Always: AGENTS.md (universal standard, read by all major AI tools)
        write_agents_md(ext, target_dir)
        for client in clients:
            write_client_config(ext, target_dir, client)

    reg = mod.ai_clients
    details = [f'{target_dir}/AGENTS.md']
    for client in clients:
        for f in reg.config_files(client):
            entry = f'{target_dir}/{f}'
            # Claude Code and OpenCode share .claude/ -- name it once.
            if entry not in details:
                details.append(entry)
    ext._guardFileWrite(
        'AI config',
        f'write AI assistant config for {ai_client_label(ext)} in '
        f'{target_dir}',
        details, _write)


def selected_clients(ext):
    """Client tokens whose config Embody should write, in registry order.

    Two menus, two questions. Configclient decides whose files are
    written; Aiclient decides what the Launch button opens. They are
    separate because they genuinely differ -- configuring Cursor while
    launching a terminal for Claude Code is a normal setup -- but each is
    a single choice, because generation is ADDITIVE: selecting a second
    client later leaves the first configured and still tracked, so
    serving several clients needs no multi-select.

    A project saved before Configclient existed has only Aiclient; fall
    back to it so its behavior is unchanged, and so an older Embody COMP
    without the parameter keeps working.
    """
    reg = mod.ai_clients
    par = getattr(ext.my.par, 'Configclient', None)
    client = par.eval() if par is not None else None
    if client not in reg.CLIENTS and client != 'none':
        client = ext.my.par.Aiclient.eval()
    return [client] if client in reg.CLIENTS else []


def write_client_config(ext, target_dir, client):
    """Write one client's rules, skills, and bespoke files."""
    row = mod.ai_clients.spec(client)
    if not row:
        return
    written = 0
    if row.get('rules'):
        written += write_rules_files(ext, target_dir, row['rules'])
    if row.get('skills'):
        written += write_skills_files(ext, target_dir, row['skills'])
    writer = row.get('writer')
    if writer == 'claudecode':
        write_claude_md(ext, target_dir)
    elif writer == 'opencode':
        write_opencode_json(ext, target_dir)
    elif writer == 'copilot':
        written += write_copilot_combined(ext, target_dir)
    elif writer == 'gemini':
        write_gemini_doc(ext, target_dir)
    block = row.get('rules') or row.get('skills')
    if written and block:
        where = block['dir'].split('/')[0]
        ext.Log(f'Generated {written} {where}/ files at {target_dir}',
                'SUCCESS')


# Delimiters for the block Embody maintains INSIDE a user-authored
# markdown file. A BEGIN/END pair, not the line-oriented '#' header used
# for .gitignore: markdown sections contain blank lines, which that
# scanner treats as the end of the block.
AGENTS_BEGIN = '<!-- BEGIN Embody/Envoy -- auto-managed, do not edit inside -->'
AGENTS_END = '<!-- END Embody/Envoy -->'

# Top-level, well-known instruction files a repo plausibly already has.
# Per-rule files (.claude/rules/*, .cursor/rules/*, ...) are NOT here:
# their names are Embody-specific, a collision is a coincidence rather
# than the norm, and skipping one of many is the right answer there.
#
# CLAUDE.md is the one a Claude Code user most often already owns, and it
# used to divert to an ENVOY.md sidecar instead -- which preserved their file
# but delivered the guidance nowhere, because nothing imports ENVOY.md and
# Claude Code does not auto-load it (contrast write_gemini_doc, which @-imports
# AGENTS.md so Gemini actually reads it). Merging puts the instructions in the
# file the tool really loads and still leaves their content untouched.
# ENVOY.md is no longer written; existing ones are left alone.
MERGEABLE_DOCS = ('CLAUDE.md', 'AGENTS.md', 'GEMINI.md',
                  '.github/copilot-instructions.md')


def _delimiter_lines(lines):
    """Indices of lines that are ONLY a delimiter, as (index, is_begin).

    Line-anchored on purpose. A substring match let a user QUOTING the
    marker in their own prose (a code fence documenting the block, say)
    act as a real delimiter, so the span from their quote to Embody's
    real END swallowed everything between -- their content, silently.
    A delimiter only counts when it is alone on its line, which is the
    only way this writer ever emits one.
    """
    found = []
    for n, line in enumerate(lines):
        # Column 0, and only trailing whitespace forgiven. Stripping
        # leading whitespace too made an INDENTED example count -- a
        # user documenting the block inside a numbered list lost the
        # lines between their example delimiters. We always emit at
        # column 0, so nothing legitimate is missed. The BOM is
        # tolerated because editors add one to the first line and it
        # would otherwise orphan the block permanently.
        bare = line.lstrip('\ufeff').rstrip()
        if bare == AGENTS_BEGIN:
            found.append((n, True))
        elif bare == AGENTS_END:
            found.append((n, False))
    return found


def strip_all_blocks(text):
    """Remove every Embody block AND any orphan delimiter line.

    Tolerates the states a real file reaches: a BEGIN whose END was
    hand-deleted, a stray END, duplicated blocks from a git merge
    resolution, and the marker quoted in the user's own prose.
    Everything between a matched pair goes; an unmatched delimiter
    loses only its own line, never the user's text.
    """
    lines = text.split('\n')
    marks = _delimiter_lines(lines)
    drop = set()
    open_at = None
    for idx, is_begin in marks:
        if is_begin:
            if open_at is not None:
                drop.add(open_at)      # previous BEGIN never closed
            open_at = idx
        elif open_at is not None:
            drop.update(range(open_at, idx + 1))
            open_at = None
        else:
            drop.add(idx)              # stray END
    if open_at is not None:
        drop.add(open_at)
    kept = [l for n, l in enumerate(lines) if n not in drop]
    return '\n'.join(kept)


def block_count(text):
    """How many WELL-FORMED Embody blocks the text holds."""
    marks = _delimiter_lines(text.split('\n'))
    total, open_ = 0, False
    for _, is_begin in marks:
        if is_begin:
            open_ = True
        elif open_:
            total += 1
            open_ = False
    return total


def merge_agents_section(existing, block):
    """Splice Embody's block into a user's file, preserving the rest.

    ONE well-formed block is replaced in place, so it keeps the position
    the user put it in. Anything else -- an orphan BEGIN, a stray END,
    duplicates from a merge conflict, the marker quoted in prose -- is
    normalized away first and one clean block is appended.
    """
    lines = existing.split('\n')
    marks = _delimiter_lines(lines)
    if (len(marks) == 2 and marks[0][1] and not marks[1][1]
            and block_count(existing) == 1):
        head = lines[:marks[0][0]]
        tail = lines[marks[1][0] + 1:]
        return '\n'.join(head + block.split('\n') + tail)
    if marks:
        existing = strip_all_blocks(existing)
    sep = '' if existing.endswith('\n\n') else (
        '\n' if existing.endswith('\n') else '\n\n')
    if not existing.strip():
        return block + '\n'
    return existing + sep + block + '\n'


# The human-readable twin of the BEGIN/END delimiters. Those are HTML
# comments -- invisible in every rendered view -- so a merged file gave the
# reader no on-page clue which half was theirs. This heading says it in prose,
# and travels inside the block so Uninstall takes it back with everything else.
BLOCK_LABEL = (
    '## Embody / Envoy -- auto-generated section\n'
    '\n'
    '*Everything from this heading down to the END marker is rewritten by\n'
    'Embody on each deploy, so edits inside it are replaced. Put your own\n'
    'instructions OUTSIDE this section -- above or below, wherever you like.\n'
    'Embody never touches those, and Uninstall removes only this block.*'
)


def agents_block(content):
    """Embody's instructions wrapped in the merge delimiters."""
    return (f'{AGENTS_BEGIN}\n{BLOCK_LABEL}\n\n'
            f'{content.strip()}\n{AGENTS_END}')


def is_generated_by_embody(ext, content):
    """True when Embody wrote this file, judged by where the marker SITS.

    Embody always puts the marker at the top of the content: either the
    first line, or the first line after the YAML frontmatter a few
    dialects require (Cursor .mdc, Copilot .instructions.md, SKILL.md).
    A plain substring test over the whole file also matched a user who
    merely QUOTED the marker in their own prose -- and that file then
    took the "ours, regenerate" path, overwriting the lot on first
    contact with no log. Position is what separates the two.
    """
    lines = content.lstrip('\ufeff').split('\n')
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    # Skip one leading frontmatter block if present.
    if i < len(lines) and lines[i].strip() == '---':
        for j in range(i + 1, len(lines)):
            if lines[j].strip() == '---':
                i = j + 1
                break
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i < len(lines) and ext._EMBODY_MARKER in lines[i]


def write_or_merge(ext, target_dir, rel_path, content):
    """Write a generated doc, MERGING when the user already owns the file.

    Three cases:
      absent, or ours (marker, no block) -> written whole, as before.
      ours-inside-theirs (our BEGIN block
        is present)                      -> block refreshed in place.
      theirs (no marker)                 -> block merged in, their content
                                            untouched.

    The block-first ordering is load-bearing: our block CONTAINS the
    generated-by marker, so on a redeploy a merged user file looks
    marker-owned and the whole-file path would overwrite everything the
    user wrote around it.

    Used for the top-level files a repo plausibly already has. Skipping
    them (the old behavior) silently left those projects with none of
    Embody's instructions and said nothing about it.
    """
    path = Path(target_dir) / rel_path
    if path.exists():
        try:
            existing = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as e:
            # UnicodeDecodeError is a ValueError, not an OSError -- it used
            # to escape here and abort the whole deploy mid-write, leaving
            # some clients configured and others not.
            ext.Log(f'Could not read {rel_path} ({e}) -- leaving it alone.',
                    'WARNING')
            return False
        had_block = AGENTS_BEGIN in existing
        if had_block or not is_generated_by_embody(ext, existing):
            merged = merge_agents_section(existing, agents_block(content))
            if merged == existing:
                return False
            try:
                ext._manifestRecordAppendedFile(
                    target_dir, path, AGENTS_BEGIN, kind='md_section')
            except Exception:
                pass

            def _write():
                path.write_text(merged, encoding='utf-8', newline='\n')
                ext.Log(
                    f'{"Updated" if had_block else "Added"} the Embody '
                    f'section in your {rel_path} -- your own content is '
                    f'untouched, and Uninstall removes only that block.',
                    'SUCCESS')

            ext._guardFileWrite(
                f'{rel_path} merge',
                f'{"update" if had_block else "add"} the Embody section in '
                f'the existing {rel_path} in {target_dir}',
                [str(path)], _write)
            return True
    return write_template(ext, target_dir, rel_path, content)


def write_agents_md(ext, target_dir):
    """Write AGENTS.md -- universal AI instructions read by all major AI tools.

    A file we own (or no file at all) is written whole, marker-gated and
    hash-tracked like every other template. A file the USER authored is
    MERGED instead: their content is untouched and Embody maintains a
    single delimited block inside it, refreshed in place on later
    deploys and stripped by Uninstall.

    Merging rather than skipping matters because AGENTS.md is the one
    file every tool reads and the one a repo most likely already has.
    Skipping it (the old behavior) silently left those projects with no
    Embody instructions at all, and overwriting it would destroy work --
    reported by a user who dragged Embody into a repo that had one.
    """
    templates_comp = ext.my.op('templates')
    agents_md_dat = templates_comp.op('text_agents_md') if templates_comp else None

    if agents_md_dat and agents_md_dat.text:
        content = agents_md_dat.text
    else:
        # Assemble from the 3 rule templates as a fallback
        ext.Log('text_agents_md DAT not found -- assembling AGENTS.md from rules', 'DEBUG')
        parts = ['<!-- Generated by Embody/Envoy -- do not edit manually -->\n']
        parts.append('# Embody + Envoy -- AI Instructions\n\n')
        parts.append(
            'This project uses [Embody](https://github.com/dylanroscover/Embody) '
            '(TouchDesigner externalization) and Envoy (MCP server for AI coding tools).\n\n'
            '---\n\n'
        )
        if templates_comp:
            for dat_name in ext._TEMPLATE_MAP_RULES:
                dat = templates_comp.op(dat_name)
                if dat and dat.text:
                    # Strip frontmatter from each rule before embedding
                    parts.append(strip_frontmatter(ext, dat.text).strip())
                    parts.append('\n\n---\n\n')
        content = ''.join(parts)

    write_or_merge(ext, target_dir, 'AGENTS.md', content)


def write_client_files(ext, target_dir, section, block):
    """Write one registry block (rules or skills) for a client.

    Returns the number of files written. Every rule dialect differs only
    in the per-file transform (rule_content), so one loop serves them
    all -- a new client with a known style needs no writer of its own.
    """
    templates_comp = ext.my.op('templates')
    if not templates_comp:
        ext.Log(f'Templates COMP not found -- skipping {block["dir"]}/ '
                f'generation', 'DEBUG')
        return 0

    is_skills = (section == 'skills')
    template_map = (ext._TEMPLATE_MAP_SKILLS if is_skills
                    else ext._TEMPLATE_MAP_RULES)
    written = 0
    for dat_name, slug in template_map.items():
        template_dat = templates_comp.op(dat_name)
        if not template_dat or not template_dat.text:
            continue
        if is_skills:
            # Frontmatter KEPT: skills need name/description -- Claude
            # Code, OpenCode, and Antigravity all validate it (and
            # require the name to match the directory slug).
            rel = f'{block["dir"]}/{slug}/SKILL.md'
            content = template_dat.text
        else:
            rel = f'{block["dir"]}/{slug}{block["ext"]}'
            content = rule_content(ext, template_dat.text, slug,
                                   block.get('style', 'strip'))
        if write_template(ext, target_dir, rel, content):
            written += 1
    return written


def write_rules_files(ext, target_dir, block):
    """Write a client's per-rule instruction files. -> count written."""
    return write_client_files(ext, target_dir, 'rules', block)


def write_skills_files(ext, target_dir, block):
    """Write a client's SKILL.md folders. -> count written."""
    return write_client_files(ext, target_dir, 'skills', block)


def rule_content(ext, raw, slug, style):
    """Transform one rule template into a client's rule dialect.

    strip   -- plain markdown, frontmatter removed (Claude Code, OpenCode,
               Antigravity); the generated-by marker stays for overwrite
               protection.
    raw     -- template verbatim, frontmatter included (Windsurf).
    cursor  -- .mdc: globs/alwaysApply injected into the EXISTING
               frontmatter rather than a duplicate block prepended.
    copilot -- applyTo frontmatter over stripped content.
    """
    if style == 'raw':
        return raw
    if style == 'strip':
        return strip_frontmatter(ext, raw)
    if style == 'copilot':
        body = strip_frontmatter(ext, raw).strip()
        return ('---\napplyTo: "**"\n---\n\n'
                '<!-- Generated by Embody/Envoy -- do not edit manually -->'
                f'\n\n{body}')
    if style == 'cursor':
        raw = raw.lstrip('\ufeff')
        SEP = '\n---\n'
        if raw.startswith('---\n') and SEP in raw[4:]:
            close_idx = raw.find(SEP, 4)
            fm_lines = raw[4:close_idx]
            rest = raw[close_idx + len(SEP):]
            if 'alwaysApply:' not in fm_lines:
                fm_lines += '\nglobs: []\nalwaysApply: true'
            return '---\n' + fm_lines + SEP + rest
        # No frontmatter -- build one from the first H1
        description = slug.replace('-', ' ').title()
        for line in raw.splitlines():
            if line.startswith('# '):
                description = line[2:].strip()
                break
        return (f'---\ndescription: "{description}"\n'
                f'globs: []\nalwaysApply: true\n---\n\n{raw}')
    return raw


def write_copilot_combined(ext, target_dir):
    """Write .github/copilot-instructions.md -- every rule in one file.

    Copilot reads this always-on file plus the per-rule
    .github/instructions/*.instructions.md the generic writer emits.
    """
    templates_comp = ext.my.op('templates')
    if not templates_comp:
        ext.Log('Templates COMP not found -- skipping .github/ generation',
                'DEBUG')
        return 0

    parts = ['<!-- Generated by Embody/Envoy -- do not edit manually -->\n\n']
    for dat_name, slug in ext._TEMPLATE_MAP_RULES.items():
        template_dat = templates_comp.op(dat_name)
        if not template_dat or not template_dat.text:
            continue
        body = strip_frontmatter(ext, template_dat.text).strip()
        heading = slug.replace('-', ' ').title()
        for line in body.splitlines():
            if line.startswith('# '):
                heading = line[2:].strip()
                break
        parts.append(f'## {heading}\n\n{body}\n\n---\n\n')

    rel = '.github/copilot-instructions.md'
    return 1 if write_or_merge(ext, target_dir, rel, ''.join(parts)) else 0


def write_gemini_doc(ext, target_dir):
    """Write GEMINI.md -- a thin @import of the always-written AGENTS.md.

    Gemini reads GEMINI.md, not AGENTS.md, so this pulls the real
    instructions in via Gemini's @file import syntax -- no duplication.
    """
    content = (
        '<!-- Generated by Embody/Envoy -- do not edit manually -->\n\n'
        '# Project Context for Gemini CLI\n\n'
        'This project uses Embody (TouchDesigner externalization) and Envoy\n'
        '(MCP server for AI coding tools). The full instructions live in\n'
        'AGENTS.md, imported below.\n\n'
        '@AGENTS.md\n'
    )
    if write_or_merge(ext, target_dir, 'GEMINI.md', content):
        ext.Log(f'Generated GEMINI.md at {target_dir}', 'SUCCESS')


def write_opencode_json(ext, target_dir):
    """Merge the Envoy entry into opencode.json (bespoke: the file also
    carries an instructions glob and a permission posture block)."""
    try:
        mod.envoy_setup.ensure_opencode_config(
            op.Embody.ext.Envoy, target_dir)
    except Exception as e:
        ext.Log(f'Could not write opencode.json: {e}', 'WARNING')


def strip_frontmatter(ext, content):
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


# --- Legacy per-client entry points ---------------------------------------
# Kept because EmbodyExt promotes them (_writeCursorRules etc.) and the
# suites call them directly; each is now its registry row plus a bespoke
# step, so the behavior is defined in exactly one place.

def write_claude_code_config(ext, target_dir):
    """Write Claude Code config: CLAUDE.md + .claude/rules/ + .claude/skills/"""
    write_claude_md(ext, target_dir)
    write_claude_rules_and_skills(ext, target_dir)


def write_claude_rules_and_skills(ext, target_dir):
    """Write .claude/rules/*.md + .claude/skills/*/SKILL.md from template
    DATs. Shared by the Claude Code and OpenCode clients: OpenCode's
    Claude-compat layer discovers .claude/skills/ natively, and the
    generated opencode.json loads .claude/rules/ via an instructions
    glob -- one copy on disk serves both clients, no drift."""
    row = mod.ai_clients.spec('claudecode')
    written = (write_rules_files(ext, target_dir, row['rules'])
               + write_skills_files(ext, target_dir, row['skills']))
    if written > 0:
        ext.Log(f'Generated {written} .claude/ files at {target_dir}',
                'SUCCESS')


def write_opencode_files(ext, target_dir):
    """Write OpenCode client config: shared .claude/ rules + skills, plus
    the Envoy entry in opencode.json.

    No CLAUDE.md is written for this client: OpenCode reads CLAUDE.md
    only as a fallback when AGENTS.md is absent, and AGENTS.md is always
    written -- the full rule set reaches OpenCode through the
    instructions glob (.claude/rules/*.md) in opencode.json instead.
    """
    write_claude_rules_and_skills(ext, target_dir)
    write_opencode_json(ext, target_dir)


def write_cursor_rules(ext, target_dir):
    """Write Cursor rules: .cursor/rules/{slug}.mdc with YAML frontmatter."""
    written = write_rules_files(ext, target_dir,
                                mod.ai_clients.spec('cursor')['rules'])
    if written > 0:
        ext.Log(f'Generated {written} .cursor/rules/ files at {target_dir}',
                'SUCCESS')


def write_copilot_instructions(ext, target_dir):
    """Write GitHub Copilot config: combined instructions + per-rule files."""
    written = (write_copilot_combined(ext, target_dir)
               + write_rules_files(ext, target_dir,
                                   mod.ai_clients.spec('copilot')['rules']))
    if written > 0:
        ext.Log(f'Generated {written} .github/ files at {target_dir}',
                'SUCCESS')


def write_windsurf_rules(ext, target_dir):
    """Write Windsurf rules: .windsurf/rules/{slug}.md (plain markdown)."""
    written = write_rules_files(ext, target_dir,
                                mod.ai_clients.spec('windsurf')['rules'])
    if written > 0:
        ext.Log(f'Generated {written} .windsurf/rules/ files at {target_dir}',
                'SUCCESS')


def write_gemini_config(ext, target_dir):
    """Write Gemini CLI config: a thin GEMINI.md that imports AGENTS.md."""
    write_gemini_doc(ext, target_dir)



def write_claude_md(ext, target_dir):
    """Write CLAUDE.md from the text_claude template DAT."""
    templates_comp = ext.my.op('templates')
    template_dat = templates_comp.op('text_claude') if templates_comp else None
    if not template_dat:
        ext.Log('CLAUDE.md template DAT not found inside Embody/templates', 'WARNING')
        return None

    content = template_dat.text
    if not content:
        ext.Log('CLAUDE.md template DAT is empty', 'WARNING')
        return None

    claude_md_path = target_dir / 'CLAUDE.md'
    existed = claude_md_path.exists()

    # Which path write_or_merge will take, decided BEFORE it runs: it logs its
    # own line for a merge, so reporting here too would say it twice.
    merging = False
    if existed:
        try:
            prev = claude_md_path.read_text(encoding='utf-8')
            merging = (AGENTS_BEGIN in prev
                       or not is_generated_by_embody(ext, prev))
        except (OSError, UnicodeDecodeError):
            merging = False   # unreadable: write_or_merge warns and bails

    # write_or_merge covers all four shapes, and its whole-file path is
    # write_template -- so a CLAUDE.md we own stays hash-tracked and
    # edit-protected exactly as before (it was the last generated markdown to
    # gain that, in v6.0.108). What changes is the user-authored case: their
    # file gets Embody's delimited block merged in rather than being skipped
    # for an ENVOY.md sidecar nothing reads.
    if write_or_merge(ext, target_dir, 'CLAUDE.md', content) and not merging:
        ext.Log(f'{"Updated" if existed else "Created"} CLAUDE.md at '
                f'{claude_md_path}', 'SUCCESS')

    return claude_md_path


# ==========================================================================
# GENERATED-FILE HASH MANIFEST + MARKER-GATED TEMPLATE WRITE (C6)
# ==========================================================================
# Sidecar manifest of {rel_path: sha256(generated content)} recorded under the
# project root. It lets write_template tell "untouched since we wrote it" (safe
# to regenerate) from "the user edited a generated file" (preserve), WITHOUT
# mutating the generated files themselves. _HASH_MANIFEST stays a class attr on
# EmbodyExt.

def content_hash(ext, content):
    """Stable 16-hex SHA-256 of a generated file's content."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]


def load_hash_manifest(ext, target_dir):
    path = Path(target_dir) / ext._HASH_MANIFEST
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_hash_manifest(ext, target_dir, manifest):
    path = Path(target_dir) / ext._HASH_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8',
        newline='\n')


# --- Install manifest -----------------------------------------------------
# Records Embody's project footprint so Uninstall/Deinit can reverse it
# PRECISELY and SAFELY -- above all, never delete a file that predated
# Embody. Additive + best-effort: a manifest write must never break the
# footprint action it records. See dev/embody/plan-init-deinit-wizard.md #6.
# _INSTALL_MANIFEST stays a class attr on EmbodyExt.

def install_manifest_skeleton(ext):
    return {'version': 1, 'files_created': [], 'files_appended': [],
            'git_config': [], 'venv': None, 'network_ops': []}


def load_install_manifest(ext, target_dir):
    path = Path(target_dir) / ext._INSTALL_MANIFEST
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            skel = install_manifest_skeleton(ext)
            if isinstance(data, dict):
                skel.update(data)  # tolerate older/partial manifests
            return skel
        except Exception:
            pass
    return install_manifest_skeleton(ext)


def save_install_manifest(ext, target_dir, manifest):
    path = Path(target_dir) / ext._INSTALL_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8',
        newline='\n')


def manifest_rel_path(ext, target_dir, path):
    """Stored form for a footprint path: POSIX-relative to the manifest root
    when the path lives under it, else an absolute POSIX path (so files
    outside the project root -- e.g. a repo .gitignore when the project is a
    subdir -- still record cleanly). Accepts a path already relative to
    target_dir OR an absolute path."""
    try:
        base = Path(target_dir).resolve()
        p = Path(path)
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
        try:
            return p.relative_to(base).as_posix()
        except ValueError:
            return p.as_posix()
    except Exception:
        return str(path).replace('\\', '/')


def manifest_record_created_file(ext, target_dir, path):
    """Record a file Embody CREATED (did not previously exist) so Uninstall
    can safely remove ONLY Embody's own additions. Best-effort; never raises."""
    try:
        rel = manifest_rel_path(ext, target_dir, path)
        m = load_install_manifest(ext, target_dir)
        if rel not in m['files_created']:
            m['files_created'].append(rel)
            save_install_manifest(ext, target_dir, m)
    except Exception as e:
        ext.Log(f'Install-manifest record failed for {path}: {e}', 'DEBUG')


def manifest_record_appended_file(ext, target_dir, path, marker, kind='block'):
    """Record a SHARED file Embody modified in place -- a git file it
    appended a marked BLOCK to (kind='block', marker = block header), or a
    JSON file it added a KEY to (kind='json_key', marker = the dotted key,
    e.g. 'mcpServers.envoy'). Uninstall reverses ONLY that unit; it NEVER
    deletes the user's file. Best-effort; never raises."""
    try:
        rel = manifest_rel_path(ext, target_dir, path)
        m = load_install_manifest(ext, target_dir)
        if not any(e.get('path') == rel for e in m['files_appended']):
            m['files_appended'].append(
                {'path': rel, 'marker': marker, 'kind': kind})
            save_install_manifest(ext, target_dir, m)
    except Exception as e:
        ext.Log(f'Install-manifest append record failed for {path}: {e}',
                'DEBUG')


def manifest_record_venv(ext, target_dir, venv_path):
    """Record that Embody CREATED the venv (safe to remove on uninstall)."""
    try:
        rel = manifest_rel_path(ext, target_dir, venv_path)
        m = load_install_manifest(ext, target_dir)
        if not m.get('venv'):
            m['venv'] = {'path': rel, 'created': True}
            save_install_manifest(ext, target_dir, m)
    except Exception as e:
        ext.Log(f'Install-manifest venv record failed: {e}', 'DEBUG')


def manifest_unrecord_created_file(ext, target_dir, path):
    """Forget a files_created record when EMBODY removed the file itself.
    A record whose file is gone but still listed marks a USER deletion --
    the tombstone _ensurePyEnvContext respects -- so self-removals must
    un-record or the rewrite after a venv reinstall would be blocked."""
    try:
        rel = manifest_rel_path(ext, target_dir, path)
        m = load_install_manifest(ext, target_dir)
        if rel in m['files_created']:
            m['files_created'].remove(rel)
            save_install_manifest(ext, target_dir, m)
    except Exception as e:
        ext.Log(f'Install-manifest unrecord failed for {path}: {e}', 'DEBUG')


def ensure_gitignore_entry(ext, git_root, entry) -> bool:
    """Append ONE entry to .gitignore's managed block iff no line anywhere
    in the file already matches it (hand-added lines count). For
    per-project additions that must not ship in MANAGED_ENTRIES -- an
    unanchored TDPyEnvManagerContext.yaml there would silently ignore
    FOREIGN contexts a fleet deliberately commits. Returns True when the
    file changed; the caller owns any consent gating."""
    try:
        gi = Path(git_root) / '.gitignore'
        lines = (gi.read_text(encoding='utf-8').splitlines()
                 if gi.exists() else [])
        # An unanchored basename line ('TDPyEnvManagerContext.yaml')
        # already ignores the file at every level -- treat it as covering
        # the anchored form, never write a near-duplicate.
        covering = {entry, entry.lstrip('/'), entry.rsplit('/', 1)[-1]}
        if any(ln.strip() in covering for ln in lines):
            return False
        # Marker strings must match envoy_setup.configure_gitignore's
        # HEADER/MARKER (the canonical managed-block writer).
        for i, ln in enumerate(lines):
            if ln.strip().startswith('# Embody / Envoy'):
                j = i + 1
                while j < len(lines) and lines[j].strip():
                    j += 1
                lines[j:j] = [entry]
                break
        else:
            if lines and lines[-1].strip():
                lines.append('')
            lines += ['# Embody / Envoy (auto-managed)', entry]
        # newline='\n': without it Windows rewrites the WHOLE file CRLF
        # (.gitattributes does not cover .gitignore; matches envoy_setup's
        # configure_gitignore).
        gi.write_text('\n'.join(lines) + '\n', encoding='utf-8',
                      newline='\n')
        ext.Log(f'.gitignore: added {entry}')
        return True
    except Exception as e:
        ext.Log(f'.gitignore entry add failed ({entry}): {e}', 'WARNING')
        return False


def manifest_record_git_config(ext, target_dir, keys):
    """Record git config keys Embody set (un-set on uninstall)."""
    try:
        if isinstance(keys, str):
            keys = [keys]
        m = load_install_manifest(ext, target_dir)
        changed = False
        for k in keys:
            if k not in m['git_config']:
                m['git_config'].append(k)
                changed = True
        if changed:
            save_install_manifest(ext, target_dir, m)
    except Exception as e:
        ext.Log(f'Install-manifest git-config record failed: {e}', 'DEBUG')


# The generated-file stamp. Embody's marker line carries the hash of the
# content below it, so a generated file PROVES on its own whether it is
# still ours:
#
#   <!-- Generated by Embody/Envoy - Do not remove this comment - sha:ab12... -->
#
# The sidecar manifest cannot do that job alone. It lives in .embody/,
# which Embody itself gitignores, so it never survives a clone -- and on
# any second machine a file with the marker and no record read as
# "written before hashing existed, regenerate", silently replacing edits
# a teammate had committed. Uninstall called the same file
# "Embody-generated, unmodified" and deleted it. The stamp travels inside
# the file, so the proof arrives with the thing it protects.
#
# The sidecar is still read for files stamped by older versions.
_STAMP_PREFIX = ' - sha:'


def stamp_marker(ext, content):
    """Return `content` with its marker line carrying the content hash."""
    lines = content.split('\n')
    idx = _marker_line_index(ext, lines)
    if idx is None:
        return content
    lines[idx] = _strip_stamp_from_line(lines[idx])
    digest = content_hash(ext, '\n'.join(lines))
    line = lines[idx]
    close = line.rfind('-->')
    if close == -1:
        return content
    lines[idx] = (line[:close].rstrip() + _STAMP_PREFIX + digest + ' '
                  + line[close:])
    return '\n'.join(lines)


def read_stamp(ext, content):
    """The hash a generated file carries, or None if it has no stamp."""
    lines = content.split('\n')
    idx = _marker_line_index(ext, lines)
    if idx is None:
        return None
    at = lines[idx].find(_STAMP_PREFIX)
    if at == -1:
        return None
    return lines[idx][at + len(_STAMP_PREFIX):].split()[0].rstrip('->').strip()


def strip_stamp(ext, content):
    """`content` with the stamp removed -- what the hash is taken over."""
    lines = content.split('\n')
    idx = _marker_line_index(ext, lines)
    if idx is None:
        return content
    lines[idx] = _strip_stamp_from_line(lines[idx])
    return '\n'.join(lines)


def _strip_stamp_from_line(line):
    at = line.find(_STAMP_PREFIX)
    if at == -1:
        return line
    close = line.rfind('-->')
    if close == -1 or close < at:
        return line[:at].rstrip()
    return line[:at].rstrip() + ' ' + line[close:]


def _marker_line_index(ext, lines):
    """Index of the marker line, skipping a leading frontmatter block."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].lstrip('\ufeff').strip() == '---':
        for j in range(i + 1, len(lines)):
            if lines[j].strip() == '---':
                i = j + 1
                break
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and ext._EMBODY_MARKER in lines[i]:
        return i
    return None


def embody_owns_unmodified(ext, existing, manifest, rel_path):
    """True when this file is still exactly what Embody last wrote.

    The file's own stamp is authoritative and travels with it. Only a
    file written before stamping existed falls back to the sidecar
    manifest, and only then can a missing record mean "assume ours".
    """
    digest = read_stamp(ext, existing)
    if digest is not None:
        return content_hash(ext, strip_stamp(ext, existing)) == digest
    stored = manifest.get(rel_path)
    if stored is None:
        return True          # pre-stamp legacy file: unchanged behavior
    return content_hash(ext, existing) == stored


def write_template(ext, target_dir, rel_path, content):
    """Write a single template file, respecting the Embody/Envoy marker.

    Overwrite policy, in order:
      - no marker             -> user-authored, never touched (skip).
      - marker + edited since
        we wrote it           -> the user changed a generated file; their
                                 edits win (skip + log; delete it to refresh).
      - marker + untouched    -> regenerate (live hash matches what we stored).
      - legacy marker (no
        tracked hash)         -> regenerate once, then it is tracked and
                                 edit-protected from here on.

    Edit-detection uses a sidecar hash manifest (_HASH_MANIFEST) so generated
    files stay byte-identical to their templates.

    Returns True if the file was written, False if skipped.
    """
    target_path = Path(target_dir) / rel_path
    was_new = not target_path.exists()  # pre-existence -> install-manifest safe-delete record
    manifest = load_hash_manifest(ext, target_dir)
    if target_path.exists():
        try:
            existing = target_path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as e:
            ext.Log(f'Could not read {rel_path} ({e}) -- leaving it alone.',
                    'WARNING')
            return False
        if not is_generated_by_embody(ext, existing):
            return False
        if not embody_owns_unmodified(ext, existing, manifest, rel_path):
            ext.Log(
                f'Kept your edits to {rel_path} '
                f'(delete the file to regenerate it from the template).',
                'INFO')
            return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # newline: Path.write_text defaults to newline=None, which translates
    # every line feed to os.linesep -- CRLF on Windows. .gitattributes
    # declares these files eol=lf, so a CRLF rewrite re-dirties every
    # generated rule and skill on EVERY deploy (empty git diff, permanent
    # 'M' in the panel), and in a user project without .gitattributes it
    # commits CRLF outright.
    content = stamp_marker(ext, content)
    target_path.write_text(content, encoding='utf-8', newline='\n')
    # Sidecar kept in step for anything still reading it.
    manifest[rel_path] = content_hash(ext, strip_stamp(ext, content))
    save_hash_manifest(ext, target_dir, manifest)
    if was_new:
        ext._manifestRecordCreatedFile(target_dir, rel_path)
    return True


def upgrade_envoy(ext):
    """Restore AI config on open if Envoy is enabled but files are missing.

    Auto restores silently (the managed default). Advanced defers with a
    breadcrumb: _extractAIConfig runs under _startup_config_pass so its guard
    DEFERS instead of popping a modal that would block the restore chain."""
    if not ext.my.par.Envoyenable.eval():
        return
    target_dir = ext._findProjectRoot()
    agents_md_missing = not (target_dir / 'AGENTS.md').exists()
    any_client_missing = any(
        client_files_missing(ext, target_dir, c)
        for c in selected_clients(ext))
    if not (agents_md_missing or any_client_missing):
        return
    prior = ext._startup_config_pass
    ext._startup_config_pass = True
    try:
        ext._extractAIConfig()
    finally:
        ext._startup_config_pass = prior


def client_files_missing(ext, target_dir, client):
    """Return True if this client's config files should be regenerated.

    The probe groups live in the registry, so a client can no longer be
    added without one -- 'vscode' and 'codex' were absent from the old
    hand-written table and silently defaulted to "nothing missing".
    """
    return mod.ai_clients.is_missing(target_dir, client)


def ai_client_label(ext):
    """Human label of the SELECTED Aiclient option (e.g. 'Claude Code') --
    NOT the parameter's own label ('AI Client')."""
    p = ext.my.par.Aiclient
    try:
        return p.menuLabels[p.menuIndex]
    except Exception:
        return p.eval()


# ==========================================================================
# INIT / RESET (C7)
# ==========================================================================

def init_envoy(ext) -> None:
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
    if not ext.my.par.Envoyenable.eval():
        ext.Log('Envoy is not enabled. Set Envoyenable = True first.', 'WARNING')
        return

    target_dir = ext._findProjectRoot()

    # MCP config (port comes from the running server, or the parameter)
    envoy = ext.my.ext.Envoy
    if ext.my.fetch('envoy_running', False):
        # Extract port from current status string
        status = str(ext.my.par.Envoystatus.eval())
        import re
        match = re.search(r'port\s+(\d+)', status)
        port = int(match.group(1)) if match else ext.my.par.Envoyport.eval()
    else:
        port = ext.my.par.Envoyport.eval()

    # ONE combined Advanced-mode confirm for the whole config regen (MCP +
    # AI); the sub-calls apply silently under _consent_bulk so this never
    # fragments into a dialog per file.
    client_label = ai_client_label(ext)

    def _apply():
        prior = ext._consent_bulk
        ext._consent_bulk = True
        try:
            envoy._configureMCPClient(port, target_dir=target_dir)
            ext._extractAIConfig()  # AI client config
            # force: InitEnvoy is the explicit re-assert path past the
            # user-deletion tombstone on the TD pre-cook venv context.
            ext._ensurePyEnvContext(force=True)
        finally:
            ext._consent_bulk = prior
        ext.Log(
            f'Envoy config regenerated for {client_label} at {target_dir}',
            'SUCCESS')

    ext._guardFileWrite(
        'AI & MCP config',
        f'(re)write MCP + AI client config for {client_label} in {target_dir}',
        [f'{target_dir}/.mcp.json', f'{target_dir}/AGENTS.md',
         f'{target_dir}/ (rules/instructions for {client_label})',
         'TDPyEnvManagerContext.yaml (TD pre-cook venv link) + its '
         '.gitignore entry'],
        _apply)


def init_git(ext) -> None:
    """Initialize or reconnect to a git repository, then generate
    git-related config files (.gitignore, .gitattributes).

    If no git repo exists, prompts the user to initialize one.
    After git is available, also regenerates MCP and AI client config
    so paths point to the git root.

    Safe to call at any time. Use this after creating a git repo
    manually, or to refresh .gitignore/.gitattributes entries.

    Requires Envoy to be enabled (par.Envoyenable = True).
    """
    if not ext.my.par.Envoyenable.eval():
        ext.Log('Envoy is not enabled. Set Envoyenable = True first.', 'WARNING')
        return

    envoy = ext.my.ext.Envoy
    git_root = envoy._checkOrInitGitRepo()

    if git_root is None:
        return  # User cancelled

    if git_root == 'no-git':
        ext.Log('No git repo -- .gitignore/.gitattributes skipped.', 'INFO')
        return

    # Store git root so Envoy can find it later (e.g. for deregistration)
    ext.my.store('_git_root', git_root)

    # Git-specific config -- ONE combined Advanced-mode confirm (sub-calls
    # apply silently under _consent_bulk so this doesn't fragment per file).
    def _apply():
        prior = ext._consent_bulk
        ext._consent_bulk = True
        try:
            envoy._configureGitignore(git_root)
            envoy._configureGitattributes(git_root)
        finally:
            ext._consent_bulk = prior
        ext.Log(f'Git config generated at {git_root}', 'SUCCESS')

    ext._guardFileWrite(
        'Git config', f'update git config in {git_root}',
        [f'{git_root}/.gitignore', f'{git_root}/.gitattributes'],
        _apply)

    # Regenerate MCP + AI config so paths point to git root (own confirm)
    ext.InitEnvoy()


def reset(ext, removeTags: bool = False) -> None:
    """Reset Embody to initial state."""
    parent.Embody.Disable(False, removeTags)
    run(f"op('{ext.my}').UpdateHandler()", delayFrames=10)
    ext.createExternalizationsTable()
    ext.my.par.externaltox = ''


# ==========================================================================
# GIT STATUS (uncommitted detection for the manager UI) (C15)
# ==========================================================================
# A SECOND status axis, distinct from "unsaved" (live-vs-disk). Externalized
# DAT scripts use TD's bidirectional syncfile, so they are always in sync with
# disk -- their only meaningful "changed" state is git-relative (on disk but
# not committed). Computed once per refresh sweep and stored at runtime (never
# written to externalizations.tsv, which would churn). Powers the orange badge
# for TOX/TDN/DAT alike. Self-disables outside a git repo.

def find_git_root_sync(ext):
    """Walk up from project.folder for a .git dir. Returns Path or 'no-git'.

    No subprocess and no prompt -- safe to call on the main-thread refresh
    sweep. Mirrors EnvoyExt._findGitRoot so the two never disagree.
    """
    project_dir = Path(project.folder).resolve()
    try:
        home_dir = Path.home().resolve()
    except Exception:
        home_dir = None
    home_is_ancestor = bool(
        home_dir and (home_dir == project_dir or home_dir in project_dir.parents))
    for parent in [project_dir] + list(project_dir.parents):
        if home_is_ancestor and parent == home_dir:
            break
        if (parent / '.git').exists():
            return parent
    return 'no-git'


def parse_git_porcelain(output: str) -> dict:
    """Parse `git status --porcelain -z` output into {repo_rel_posix: code}.

    `-z` means NUL-separated records and NO path quoting, so paths with
    spaces/unicode are handled cleanly. A rename/copy record (X or Y in
    R/C) is followed by an extra NUL-separated origin path; both the new and
    origin paths are recorded (membership tests only ever hit the one that
    currently exists on disk). Untracked entries (`??`) count as changed.
    """
    result = {}
    tokens = output.split('\0')
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok or len(tok) < 3 or tok[2] != ' ':
            i += 1
            continue
        code, path = tok[:2], tok[3:]
        if path:
            result[path] = code
        if code[0] in ('R', 'C'):
            i += 1
            if i < n and tokens[i]:
                result[tokens[i]] = code
        i += 1
    return result


def map_changed_to_ops(changed, project_prefix, rows):
    """Map a git {repo_rel_posix: code} set to {op_path: code} for externalized
    rows.

    `project_prefix` is project.folder relative to the git root as a posix
    prefix ('' or e.g. 'dev/'); `rows` is an iterable of
    (op_path, rel_file_path). Pure string math -- no filesystem and no TD
    access -- so it is both fast (no per-row Path.resolve) and unit-testable.
    """
    out = {}
    if not changed:
        return out
    for path, rel in rows:
        if not path or not rel or path == '/':
            continue
        code = changed.get(project_prefix + rel.replace('\\', '/'))
        if code:
            out[path] = code
    return out


def row_is_unsaved(dirty_val) -> bool:
    """Whether a manager row has unsaved in-TD changes (`dirty` or `Par`).
    Single source of truth for the manager's "dirty" filter keyword (used
    by inject_parents)."""
    return str(dirty_val) in ('True', 'true', '1', 'Par')


def row_has_changes(dirty_val, uncommitted) -> bool:
    """Whether a manager row has pending changes on EITHER axis: unsaved
    (`dirty`/`Par`) or git-uncommitted. Single source of truth for the
    manager's "changed" filter keyword (used by inject_parents)."""
    return row_is_unsaved(dirty_val) or bool(uncommitted)


def update_git_status(ext) -> None:
    """Kick off an ASYNC git-uncommitted scan on a worker thread; never blocks
    the refresh frame.

    The `git status` subprocess (tens of ms) and parsing run off the main
    thread; the cheap, string-based mapping + store happen back on the main
    thread in the SuccessHook closure, which then refreshes the manager
    badges. Runtime-only via store('git_status', ...) (never touches
    externalizations.tsv). No git repo, thread-pool exhaustion, or any failure
    -> empty/unchanged map; the orange indicator simply does not show.

    Each scan carries a generation id captured in its hooks, and its result
    state is task-local (closure, not a shared attribute). Only the LATEST
    generation publishes or clears the in-flight flag -- so a stale task that
    finally fires after a re-arm is a no-op and cannot clobber a newer scan.
    """
    import time
    now = time.monotonic()
    # Coalesce: one scan in flight at a time. Re-arm only if a prior scan has
    # been "running" implausibly long (worker died without a hook firing).
    if getattr(ext, '_git_check_running', False) and \
            (now - getattr(ext, '_git_check_started', 0)) < 10:
        return
    # Bump the generation: this supersedes any still-pending stale task.
    gen = getattr(ext, '_git_gen', 0) + 1
    ext._git_gen = gen
    git_root = find_git_root_sync(ext)
    if git_root == 'no-git':
        ext._git_check_running = False
        ext.my.store('git_status', {})
        return
    proj = str(Path(project.folder).resolve())
    git_root_s = str(git_root)
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in ('GIT_DIR', 'GIT_WORK_TREE',
                     'GIT_INDEX_FILE', 'GIT_CEILING_DIRECTORIES')}
    # Never take the optional index lock -- a background status must not
    # contend with the user's (or an agent's) concurrent git add/commit.
    clean_env['GIT_OPTIONAL_LOCKS'] = '0'
    state = {'changed': None}
    ext._git_check_running = True
    ext._git_check_started = now

    # Worker runs on a pool thread -- captures only locals + a pure module
    # function (resolved here on the main thread), so it touches NO TD objects
    # and never `mod` off-main (the one sanctioned main->worker handoff).
    parse = parse_git_porcelain

    def worker():
        try:
            # --no-optional-locks: never write .git/index (no lock contention).
            # --untracked-files=all: enumerate files inside new dirs (not `?? dir/`).
            r = subprocess.run(
                ['git', '--no-optional-locks', 'status', '--porcelain', '-z',
                 '--untracked-files=all', '--', proj],
                cwd=git_root_s, capture_output=True, text=True,
                encoding='utf-8', errors='replace',
                env=clean_env, stdin=subprocess.DEVNULL, timeout=5,
                check=False, creationflags=NO_WINDOW)
            state['changed'] = parse(r.stdout or '') if r.returncode == 0 else {}
        except Exception:
            state['changed'] = {}

    def done():
        # Only the latest generation publishes / clears the flag.
        if gen != getattr(ext, '_git_gen', None):
            return
        ext._git_check_running = False
        try:
            prefix = Path(proj).relative_to(Path(git_root_s)).as_posix()
            prefix = (prefix + '/') if prefix and prefix != '.' else ''
        except Exception:
            prefix = ''
        rows = []
        table = ext.Externalizations
        if table is not None:
            for r in range(1, table.numRows):
                rows.append((ext._cellVal(r, 'path'),
                             ext._cellVal(r, 'rel_file_path')))
        ext.my.store('git_status',
                     map_changed_to_ops(state['changed'] or {}, prefix, rows))
        # Refresh the manager so the orange badges reflect the new git state.
        try:
            ext.my.op('list/inject_parents').cook(force=True)
            ext.lister.reset()
        except Exception:
            pass

    def failed(e):
        if gen != getattr(ext, '_git_gen', None):
            return
        ext._git_check_running = False
        ext.Log(f"Git status worker failed: {e}", "DEBUG")

    tm = op.TDResources.ThreadManager
    task = tm.TDTask(target=worker, SuccessHook=done, ExceptHook=failed)
    if tm.EnqueueTask(task, standalone=True) is None:
        # Thread pool at capacity -- abandon; the next refresh retries.
        ext._git_check_running = False
