"""EmbodyExt uninstall + release .toe export + settings persistence (module DAT).

Module DAT (mod.embody_admin) called by EmbodyExt on the MAIN THREAD only (the
ext-diet WP7c + WP7d clusters C5 + C9). Holds:

  - C5  Uninstall: the NON-DESTRUCTIVE planner (compute_uninstall_plan) +
        preview_uninstall, and the DESTRUCTIVE executor (execute_uninstall_plan)
        with its file-safe helpers (remove_tree_within / strip_marked_block /
        strip_mcp_envoy) + the promoted Uninstall entry point. The uninstall
        marker constants (_UNINSTALL_MARKER_*) live at module scope -- their sole
        consumer is compute_uninstall_plan and nothing external reads them.
  - C5b Release .toe export: the PURE planners (plan_release_*) and the
        orchestrator (preview_release_toe / export_release_toe) behind
        PreviewReleaseToe / ExportReleaseToe. Steps 4-6 leave this module
        entirely -- they run from a generated run() script, because this
        DAT lives inside the COMP they delete.
  - C9  Settings/config.json persistence: settings_path / find_settings_file /
        project_json_path / write_project_json / save_settings /
        defer_save_settings / restore_settings / show_tdxn_migration_nudge.
        Plus the project.json 'convoy' key steward (Convoy Phase 2):
        mint_convoy_id / read_convoy_entry / read_convoy_id /
        read_convoy_binding_state / ensure_convoy_id / adopt_convoy_id,
        which own that ONE key with the same key-level discipline
        write_project_json applies to td_build.

EmbodyExt keeps a thin delegating stub for every function here (identical
signatures; promoted names stay UpperCamelCase). No module-level TD access --
each function takes the ext instance (`ext`) and reaches TD through it
(ext.Log, ext.my, ext._findProjectRoot, ...) or through the TD globals (op,
project, run, parent, app, ui, ParMode) available inside the bodies at
main-thread call time.

THREAD NOTE: every function here is main-thread. Uninstall is promoted API
(interactive / wizard / tests); the settings restore/save path is driven by
execute.py (onCreate/onStart via run()) and parexec.py (_deferSaveSettings on a
param change), all main-thread. No worker touches these -- mod.* delegation is
therefore legal throughout.

DISPATCH CONTRACT: intra-cluster calls are module-local (no unit test
monkeypatches any name in this cluster -- verified). Cross-module hops go through
the facade via ext.*: _loadInstallManifest / _loadHashManifest (embody_git stubs
on the facade), and the spine/retained methods _findProjectRoot / _rootForMode /
_venvPaths / _uninstallClassifyMarker / _messageBox / _getTDXNStrategyComps /
_applyTdxnModeGating. The class attr _PERSISTED_PARAMS stays on EmbodyExt (read by
parexec.py) and is reached via ext._PERSISTED_PARAMS. Instance state
(_settings_save_pending, _restoring_settings) lives on the ext, unchanged.

The run() deferral strings target the facade stubs (ext.Embody._saveSettings /
._showTDXNMigrationNudge / ext.Envoy.Start) so they resolve after the move.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
from pathlib import Path
from typing import Optional

# No console flash over TD's GUI -- see embody_git.NO_WINDOW for the why.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ==========================================================================
# UNINSTALL / DEINIT (C5)
# ==========================================================================
# A reversible teardown of Embody's project footprint. compute_uninstall_plan is
# the NON-DESTRUCTIVE planner: it reads the install manifest (precise) plus a
# marker-scan fallback (pre-manifest installs) and returns exactly what a full
# Uninstall WOULD do -- nothing is removed there. The executor consumes THAT
# plan, so the preview can never drift from what runs. Conservative: anything not
# provably Embody-owned is classed 'review' (KEPT + flagged), never silently
# deleted. See dev/embody/plan-init-deinit-wizard.md sec 5.

# Derived from the ai_clients registry, NOT hand-listed. These were the
# last copies of the per-client tables the registry replaced, and they had
# already gone stale: no .agents/rules, no .agents/skills, so every
# Antigravity rule and skill orphaned on any install whose manifest was
# missing -- which is exactly when the marker scan is the only thing left.
# Module-level constants would need TD at import time, so they are
# functions the planner calls.

def _uninstall_marker_files():
    return tuple(['AGENTS.md'] + [f for f in mod.ai_clients.cleanup_files()
                                  if '/' not in f])


def _uninstall_marker_trees():
    return tuple(mod.ai_clients.cleanup_sweep_dirs())


def _uninstall_marker_singles():
    return tuple(f for f in mod.ai_clients.cleanup_files() if '/' in f)


def compute_uninstall_plan(ext, target_dir=None):
    """NON-DESTRUCTIVE. Return exactly what Uninstall would remove/strip so
    it can be reviewed before any deletion. Manifest-driven, with a
    marker-scan fallback for pre-manifest installs.

    Plan dict:
      root, sources[list],
      delete  [{path,kind,why}]        provably Embody's -> remove
      strip   [{path,kind,marker,why}] git block / .mcp.json key -> reverse only that
      unset   [keys]                   repo git config to un-set
      review  [{path,why}]             exists, not provably ours -> KEPT, flagged
      missing [paths]                  recorded but already gone
    """
    root = (Path(target_dir).resolve() if target_dir
            else Path(ext._findProjectRoot()).resolve())
    m = ext._loadInstallManifest(str(root))
    hashes = ext._loadHashManifest(str(root))
    plan = {'root': str(root), 'sources': [],
            'delete': [], 'strip': [], 'unset': [], 'review': [], 'missing': []}
    seen = set()

    def _abs(stored):
        p = Path(stored)
        return p if p.is_absolute() else (root / p)

    def _rel(p):
        try:
            return p.resolve().relative_to(root).as_posix()
        except Exception:
            return str(p)

    def _add(bucket, p, **kw):
        rp = str(p.resolve() if hasattr(p, 'resolve') else p)
        if rp in seen:
            return False
        seen.add(rp)
        entry = {'path': _rel(p)}
        entry.update(kw)
        plan[bucket].append(entry)
        return True

    def _add_strip(p, kind, marker, why):
        rp = str(p.resolve())
        if any(s['path'] == _rel(p) for s in plan['strip']):
            return
        seen.add(rp)
        plan['strip'].append({'path': _rel(p), 'kind': kind,
                              'marker': marker, 'why': why})

    def _classify_into(p):
        cls = ext._uninstallClassifyMarker(p, root, hashes)
        if cls == 'delete':
            _add('delete', p, kind='file', why='Embody-generated, unmodified')
        elif cls == 'merged':
            # The user's own file with Embody's block spliced in: take the
            # block, never the file.
            _add_strip(p, 'md_section', mod.embody_git.AGENTS_BEGIN,
                       'remove only the Embody section; your file is kept')
        elif cls == 'review':
            _add('review', p, why='you edited this generated file -- kept')

    # ---- manifest (precise) ----
    if any((m.get('files_created'), m.get('files_appended'),
            m.get('git_config'), m.get('venv'))):
        plan['sources'].append('manifest')

    mcp_specs = {s['path']: s for s in mod.ai_clients.project_mcp_specs()}

    def _strip_reason(spec):
        if spec['style'] == 'opencode':
            return ('remove Embody server + instructions entry; '
                    'delete file only if nothing else remains')
        return 'remove Embody server; delete file only if none remain'

    for stored in m.get('files_created', []):
        p = _abs(stored)
        spec = mcp_specs.get(_rel(p))
        if spec is not None:
            if p.exists():
                _add_strip(p, 'mcp_config', f'{spec["key"]}.envoy',
                           _strip_reason(spec))
            continue
        if not p.exists():
            plan['missing'].append(stored); continue
        if p.name == 'settings.local.json':
            _add('review', p,
                 why='created by Embody but may hold your permission edits -- kept')
            continue
        if p.name == mod.embody_pyenv.TD_CONTEXT_FILENAME:
            # No marker survives TD's rewrite of this file (yaml.safe_dump
            # strips comments) -- classify semantically instead (2026-08-20).
            if mod.embody_pyenv.classify_td_context(
                    str(p.parent), str(p.parent / '.venv')) == 'ours':
                _add('delete', p, kind='file',
                     why='TD pre-cook venv context authored by Embody')
            else:
                _add('review', p,
                     why='recorded by Embody but no longer points at its '
                         '.venv -- kept')
            continue
        _classify_into(p)

    for e in m.get('files_appended', []):
        p = _abs(e['path'])
        if not p.exists():
            plan['missing'].append(e['path']); continue
        _add_strip(p, e.get('kind', 'block'), e.get('marker', ''),
                   "strip only Embody's block/key -- your file is kept")

    plan['unset'] = list(m.get('git_config', []))

    v = m.get('venv')
    if v:
        p = _abs(v['path'])
        if p.exists():
            # Name the user's declared extras in the plan (2026-08-19):
            # "Embody created it, so Embody removes it" predates user
            # packages living in this venv -- deleting a 3 GB torch
            # install deserves an explicit line in the preview. The
            # declaration survives in .embody/project.json either way,
            # so a reinstall brings the extras back.
            why = 'Embody-created virtual environment'
            try:
                extras = ext._declaredExtras()
                if extras:
                    why += (f' -- CARRIES {len(extras)} user package(s) '
                            f'({", ".join(extras[:6])}'
                            f'{", ..." if len(extras) > 6 else ""}); the '
                            f'declaration in .embody/project.json is kept, '
                            f'so reinstalling Embody restores them')
            except Exception:
                pass
            _add('delete', p, kind='dir', why=why)
        else:
            plan['missing'].append(v['path'])

    # ---- marker-scan FALLBACK (pre-manifest installs / anything missed) ----
    before = sum(len(plan[b]) for b in ('delete', 'review', 'strip')) + len(plan['unset'])
    for name in _uninstall_marker_files():
        p = root / name
        if p.is_file():
            _classify_into(p)
    for sub in _uninstall_marker_trees():
        d = root / sub
        if d.is_dir():
            for p in d.rglob('*'):
                if p.is_file():
                    _classify_into(p)
    for single in _uninstall_marker_singles():
        p = root / single
        if p.is_file():
            _classify_into(p)
    # Every client's own MCP config, from the registry -- .mcp.json is only
    # Claude Code's, and a project configured for Cursor/VS Code/Gemini/
    # Codex/Antigravity has an envoy entry in files this scan used to miss.
    for spec in mcp_specs.values():
        cfg_path = root / spec['path']
        if not cfg_path.is_file() or str(cfg_path.resolve()) in seen:
            continue
        try:
            text = cfg_path.read_text(encoding='utf-8')
            if spec['style'] == 'toml':
                present = f'[{spec["key"]}.envoy]' in text
            else:
                present = 'envoy' in (
                    json.loads(text).get(spec['key']) or {})
            if present:
                _add_strip(cfg_path, 'mcp_config', f'{spec["key"]}.envoy',
                           _strip_reason(spec))
        except Exception:
            pass
    # Directories Embody created to hold generated files. Planned as
    # 'emptydir' so they appear in the preview the user confirms -- the
    # executor uses a bare rmdir, which FAILS on a non-empty directory, so
    # anything of the user's still inside keeps the directory alive.
    # Deepest-first, from the registry.
    for sub in mod.ai_clients.cleanup_dirs():
        dp = root / sub
        if not dp.is_dir():
            continue
        # A skills tree holds one directory PER SKILL; those have to go
        # before their parent can rmdir. Nested children first.
        for child in sorted(dp.iterdir(), key=lambda c: c.name):
            if child.is_dir():
                _add('delete', child, kind='emptydir',
                     why='Embody-generated skill folder -- removed if empty')
        _add('delete', dp, kind='emptydir',
             why='created by Embody -- removed only if empty afterwards')

    for gname, marker in (('.gitignore', '# Embody / Envoy'),
                          ('.gitattributes', 'Embody / Envoy')):
        gp = root / gname
        if gp.is_file() and str(gp.resolve()) not in seen:
            try:
                if marker in gp.read_text(encoding='utf-8'):
                    _add_strip(gp, 'block', marker,
                               "strip only Embody's block -- your file is kept")
            except Exception:
                pass
    # git config (read-only query) for pre-manifest installs
    if not plan['unset']:
        # Both spellings: the driver was renamed tdn -> tdxn in 6.2.35,
        # and an install that never re-ran setup still carries the old key.
        for key in ('diff.tdxn.textconv', 'diff.tdxn.cachetextconv',
                    'diff.tdn.textconv', 'diff.tdn.cachetextconv'):
            try:
                r = subprocess.run(['git', 'config', '--get', key],
                                   cwd=str(root), capture_output=True,
                                   text=True, timeout=5,
                                   encoding='utf-8', errors='replace',
                                   stdin=subprocess.DEVNULL,
                                   creationflags=NO_WINDOW)
                if r.returncode == 0 and (r.stdout or '').strip():
                    plan['unset'].append(key)
            except Exception:
                pass
    # venv not captured by the manifest -> flag for review (can't prove
    # Embody created it without the record, so never auto-delete it). Prefer
    # the authoritative venv location (under project.folder) -- which can sit
    # in a subdir of the manifest root -- but ONLY when it falls under the
    # root being planned, so a plan for an unrelated root (a test/other
    # project) never picks up the LIVE project's venv.
    venv_dir = root / '.venv'
    try:
        cand = Path(ext._venvPaths()['venv_dir']).resolve()
        cand.relative_to(root)  # raises if not under this root
        venv_dir = cand
    except Exception:
        pass
    if venv_dir.is_dir() and str(venv_dir.resolve()) not in seen:
        _add('review', venv_dir, kind='dir',
             why="looks like Embody's virtualenv but was not recorded -- review before removing")
    if sum(len(plan[b]) for b in ('delete', 'review', 'strip')) + len(plan['unset']) > before:
        plan['sources'].append('fallback')

    # ---- .embody/ (Embody-owned state) removable wholesale ----
    embody_dir = root / '.embody'
    if embody_dir.is_dir():
        _add('delete', embody_dir, kind='dir',
             why='Embody runtime state (manifest, bridge, config, hashes)')

    return plan


def preview_uninstall(ext, target_dir=None):
    """Log + return a NON-DESTRUCTIVE preview of a full Uninstall. Nothing is
    removed. Use this to review the reversal plan before running Uninstall."""
    plan = compute_uninstall_plan(ext, target_dir)
    src = ', '.join(plan['sources']) or 'none -- nothing recorded/found'
    lines = [f'Uninstall preview for {plan["root"]} (sources: {src})']
    if plan['delete']:
        lines.append(f'  REMOVE ({len(plan["delete"])}):')
        for a in plan['delete']:
            lines.append(f'    - {a["path"]}  [{a.get("kind","file")}] -- {a["why"]}')
    if plan['strip']:
        lines.append(f'  MODIFY ({len(plan["strip"])}) -- your file kept, only Embody\'s part reversed:')
        for a in plan['strip']:
            lines.append(f'    ~ {a["path"]}  ({a["kind"]}: {a["marker"]})')
    if plan['unset']:
        lines.append(f'  GIT CONFIG un-set: {", ".join(plan["unset"])}')
    if plan['review']:
        lines.append(f'  REVIEW ({len(plan["review"])}) -- KEPT (may hold your edits):')
        for a in plan['review']:
            lines.append(f'    ? {a["path"]} -- {a["why"]}')
    if plan['missing']:
        lines.append(f'  already gone: {len(plan["missing"])}')
    ext.Log('\n'.join(lines), 'INFO')
    return plan


# ---- executor (destructive) -- consumes a plan from compute_uninstall_plan ----

def remove_tree_within(ext, path, root):
    """Recursively remove a directory, but ONLY if it resolves INSIDE root
    (guard against a catastrophic path). Bottom-up unlink + rmdir -- a
    scoped, explicit walk, never a blind rmtree of an arbitrary path.
    Returns files removed."""
    path = Path(path).resolve()
    root = Path(root).resolve()
    try:
        path.relative_to(root)  # raises if path is not under root
    except ValueError:
        ext.Log(f'Uninstall: refusing to remove {path} -- outside {root}',
                'WARNING')
        return 0
    if not path.is_dir():
        return 0
    removed = 0
    # deepest-first so a dir is empty by the time we rmdir it
    for child in sorted(path.rglob('*'),
                        key=lambda p: len(p.parts), reverse=True):
        try:
            if child.is_file() or child.is_symlink():
                child.unlink(); removed += 1
            elif child.is_dir():
                child.rmdir()
        except OSError as e:
            ext.Log(f'Uninstall: could not remove {child}: {e}', 'DEBUG')
    try:
        path.rmdir()
    except OSError as e:
        ext.Log(f'Uninstall: could not remove {path}: {e}', 'DEBUG')
    return removed


def strip_marked_block(ext, text, marker):
    """Return text with Embody's marked comment block removed -- the header
    comment line containing `marker` plus its consecutive non-blank entry
    lines (and a single preceding blank separator). User content is kept."""
    lines = text.split('\n')
    out = []
    i, n = 0, len(lines)
    while i < n:
        if marker in lines[i] and lines[i].lstrip().startswith('#'):
            if out and out[-1] == '':
                out.pop()               # drop the blank separator we added
            i += 1
            while i < n and lines[i].strip() != '':
                i += 1                  # skip the block's entry lines
            continue
        out.append(lines[i]); i += 1
    return '\n'.join(out)


def strip_md_section(ext, text):
    """Remove Embody's delimited block from a user-authored markdown file.

    The counterpart of embody_git.merge_agents_section: everything the
    user wrote survives, only the BEGIN..END span goes. Returns the text
    unchanged when no block is present.
    """
    git = mod.embody_git
    if git.AGENTS_BEGIN not in text and git.AGENTS_END not in text:
        return text
    # Same tolerant scanner the writer uses: a duplicated or half-deleted
    # block must not leave one behind, and an orphan delimiter must not
    # take the user's prose with it.
    out = git.strip_all_blocks(text)
    if not out.strip():
        return ''
    return out.rstrip('\n') + '\n'


def mcp_spec_for(path):
    """The registry MCP spec this path is an instance of, if any.

    Matched on the whole relative path, never the basename: Cursor's
    .cursor/mcp.json and VS Code's .vscode/mcp.json share a filename but
    key their servers differently ('mcpServers' vs 'servers'), so a
    basename match would strip the wrong key and silently leave the entry
    behind. Deepest match wins.
    """
    posix = Path(path).as_posix()
    best = None
    for spec in mod.ai_clients.project_mcp_specs():
        rel = spec['path']
        if posix == rel or posix.endswith('/' + rel):
            if best is None or rel.count('/') > best['path'].count('/'):
                best = spec
    return best


def strip_toml_envoy(ext, path, key):
    """Remove only the [<key>.envoy] table from a TOML config (Codex).

    Surgical for the same reason the writer is: the rest of the user's
    config.toml must survive untouched, not be reformatted by a
    round-trip. Deletes the file only if nothing else remains.
    """
    header = f'[{key}.envoy]'
    try:
        lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    except OSError:
        return
    start = next((i for i, l in enumerate(lines) if l.strip() == header), None)
    if start is None:
        return
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith('['):
            end = i
            break
    remaining = ''.join(lines[:start] + lines[end:])
    if remaining.strip():
        path.write_text(remaining, encoding='utf-8', newline='\n')
    else:
        path.unlink()


def strip_mcp_envoy(ext, path):
    """Remove only Embody's entries from a client's MCP config.

    Shape comes from the ai_clients registry: each client keys its server
    map differently (.mcp.json and Cursor use 'mcpServers', VS Code uses
    'servers', opencode.json uses 'mcp', Codex uses a TOML table), and one
    shape's keys must never be written into another's file. opencode.json
    additionally carries Embody's generated '.claude/rules/*.md'
    instructions entry (see envoy_setup.write_opencode_config). Delete the
    file only if stripping leaves nothing but boilerplate ('$schema') --
    the user's servers and keys are always preserved.
    """
    spec = mcp_spec_for(path)
    if spec is not None and spec['style'] == 'toml':
        strip_toml_envoy(ext, path, spec['key'])
        return
    try:
        # utf-8-sig for the same reason the writer uses it: a BOM would
        # otherwise leave the Envoy entry in the file forever.
        cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return
    is_opencode = path.name == 'opencode.json' or (
        'mcp' in cfg and 'mcpServers' not in cfg)
    if spec is not None:
        key = spec['key']
    else:
        key = 'mcp' if is_opencode else 'mcpServers'
    servers = cfg.get(key, {})
    servers.pop('envoy', None)
    if is_opencode:
        # Remove Embody's generated instructions entry (only ours -- the
        # exact glob write_opencode_config appends).
        instr = cfg.get('instructions')
        if isinstance(instr, list):
            keep = [e for e in instr if e != '.claude/rules/*.md']
            if keep:
                cfg['instructions'] = keep
            else:
                cfg.pop('instructions', None)
        # A fresh Embody-created file also carried our permission block;
        # remove it only when nothing else meaningful remains (a user may
        # have edited it -- then the file survives below and keeps it).
    if servers:
        cfg[key] = servers
        path.write_text(json.dumps(cfg, indent=2) + '\n',
                        encoding='utf-8', newline='\n')
        return
    cfg.pop(key, None)
    leftover = [k for k in cfg if k not in ('$schema',)]
    if is_opencode and leftover == ['permission']:
        # Only Embody's fresh-file permission block remains -> ours.
        leftover = []
    if leftover:
        path.write_text(json.dumps(cfg, indent=2) + '\n',
                        encoding='utf-8', newline='\n')
    else:
        path.unlink()  # the file held only Embody's config -> remove it


def execute_uninstall_plan(ext, plan, include_review=False):
    """Execute a plan from compute_uninstall_plan. Filesystem + git only.
    'review' items are KEPT unless include_review=True. Returns a summary
    dict. This is the one place that actually removes/modifies files."""
    root = Path(plan['root'])

    def _abs(rel):
        p = Path(rel)
        return p if p.is_absolute() else (root / p)

    summary = {'deleted': 0, 'stripped': 0, 'unset': 0,
               'kept_review': 0, 'errors': 0}

    def _remove(entry):
        p = _abs(entry['path'])
        try:
            if entry.get('kind') == 'emptydir':
                # Bare rmdir on purpose: it raises on a non-empty
                # directory, so a directory still holding the user's own
                # files is never removed.
                if p.is_dir():
                    try:
                        p.rmdir()
                        summary['deleted'] += 1
                    except OSError:
                        pass          # not empty -> the user's, leave it
            elif entry.get('kind') == 'dir':
                if p.is_dir():
                    remove_tree_within(ext, p, root)
                    summary['deleted'] += 1
            elif p.exists():
                p.unlink(); summary['deleted'] += 1
        except OSError as e:
            summary['errors'] += 1
            ext.Log(f'Uninstall: could not remove {p}: {e}', 'WARNING')

    for entry in plan['delete']:
        if entry.get('kind') != 'emptydir':
            _remove(entry)

    for a in plan['strip']:
        p = _abs(a['path'])
        if not p.exists():
            continue
        try:
            # 'toml_table' is Codex's config.toml: it MUST route to the
            # shape-aware stripper like the JSON kinds. Falling through to
            # strip_marked_block (a '#'-header line scanner built for
            # .gitignore) silently left the envoy entry in place forever --
            # the file was planned for stripping and then not stripped.
            if a['kind'] in ('mcp_config', 'json_key', 'toml_table'):
                strip_mcp_envoy(ext, p)
            elif a['kind'] == 'md_section':
                # A user-authored AGENTS.md we merged into: take back only
                # our delimited block, never the file.
                # Same newline discipline as the writer: the reverser
                # must not hand back a CRLF-converted file.
                original = p.read_text(encoding='utf-8')
                stripped = strip_md_section(ext, original)
                if not stripped.strip():
                    # Nothing but Embody's block was ever in it. The root-move
                    # sweep unlinks in this case; leaving a 0-byte file here
                    # would be a different answer to the same question.
                    p.unlink()
                    summary['deleted'] += 1
                elif stripped != original:
                    p.write_text(stripped, encoding='utf-8',
                                 newline='\n')
            else:
                p.write_text(
                    strip_marked_block(
                        ext, p.read_text(encoding='utf-8'), a['marker']),
                    encoding='utf-8', newline='\n')
            summary['stripped'] += 1
        except OSError as e:
            summary['errors'] += 1
            ext.Log(f'Uninstall: could not strip {p}: {e}', 'WARNING')

    # Directories last of all: a bare rmdir needs the contents gone, and
    # the strip pass above is what removes the final file in .cursor/,
    # .vscode/, .gemini/, .codex/ and .agents/. Plan order is
    # deepest-first, so children are attempted before their parents.
    for entry in plan['delete']:
        if entry.get('kind') == 'emptydir':
            _remove(entry)

    for key in plan['unset']:
        try:
            subprocess.run(['git', 'config', '--unset', key],
                           cwd=str(root), capture_output=True, text=True,
                           encoding='utf-8', errors='replace',
                           timeout=5, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
            summary['unset'] += 1
        except Exception as e:
            ext.Log(f'Uninstall: could not un-set {key}: {e}', 'DEBUG')

    if include_review:
        for entry in plan['review']:
            _remove(entry)
    else:
        summary['kept_review'] = len(plan['review'])

    return summary


def uninstall(ext, confirm=False, include_review=False, target_dir=None):
    """Reverse Embody's project footprint. DESTRUCTIVE -- requires
    confirm=True (review PreviewUninstall() first). Stops Envoy, then
    removes/strips per the plan. 'review' items (files you edited, an
    unrecorded venv) are KEPT unless include_review=True; user files are
    never deleted -- only Embody's own additions."""
    if not confirm:
        ext.Log('Uninstall is destructive. Review PreviewUninstall() first, '
                'then call Uninstall(confirm=True). Nothing was changed.',
                'WARNING')
        return {'ran': False, 'reason': 'confirm required'}
    plan = compute_uninstall_plan(ext, target_dir)
    try:  # stop Envoy so its venv/config aren't in use during removal
        if ext.my.par.Envoyenable.eval():
            ext.my.par.Envoyenable = 0
    except Exception:
        pass
    summary = execute_uninstall_plan(ext, plan, include_review=include_review)
    summary['ran'] = True
    ext.Log(f'Uninstall complete: {summary}', 'SUCCESS')
    return summary


def uninstall_handler(ext, target_dir=None):
    """Interactive Uninstall (the Uninstall pulse handler). Computes the
    NON-DESTRUCTIVE plan, explains it in a ui.messageBox, and only reverses
    the footprint when the user confirms. 'review' items (files you edited,
    an unrecorded venv) are KEPT. Uses ext._messageBox so a save/test context
    gets the safe Cancel default (-1) instead of a modal freeze."""
    plan = compute_uninstall_plan(ext, target_dir)
    n_del = len(plan['delete'])
    n_strip = len(plan['strip'])
    n_unset = len(plan['unset'])
    n_review = len(plan['review'])

    if not (n_del or n_strip or n_unset):
        ext._messageBox(
            'Embody -- Uninstall',
            'Nothing to uninstall: no Embody-generated files, git config, or '
            'MCP / AI-assistant config were found at this project root:\n'
            f'{plan["root"]}',
            buttons=['OK'])
        ext.Log('Uninstall: nothing to remove at '
                f'{plan["root"]}.', 'INFO')
        return {'ran': False, 'reason': 'nothing to uninstall'}

    lines = ['Remove Embody from this project?',
             f'Root: {plan["root"]}',
             '',
             'Only files Embody created are removed -- your own files are '
             'never deleted.',
             '']
    if n_del:
        lines.append(
            f'- REMOVE {n_del} Embody-generated item(s): AI-assistant config '
            "(CLAUDE.md / AGENTS.md / .claude / .cursor / ...), Embody's "
            '.venv, and the .embody/ state folder.')
    if n_strip:
        lines.append(
            f'- MODIFY {n_strip} shared file(s) -- strip ONLY Embody\'s block '
            '/ key (.gitignore, .gitattributes, .mcp.json). Your content is '
            'kept.')
    if n_unset:
        lines.append(
            f'- UN-SET {n_unset} git config key(s) (the .tdn diff driver).')
    if n_review:
        lines.append(
            f'- KEEP {n_review} item(s) you may have edited (flagged, left '
            'untouched).')
    lines += ['',
              'Your externalized .tox / .tdn / .py files and the Embody COMP '
              'itself are NOT removed. This cannot be undone.']

    choice = ext._messageBox(
        'Embody -- Uninstall', '\n'.join(lines),
        buttons=['Cancel', 'Uninstall'])
    if choice != 1:
        ext.Log('Uninstall cancelled -- nothing was changed.', 'INFO')
        return {'ran': False, 'reason': 'cancelled'}

    summary = uninstall(ext, confirm=True, target_dir=target_dir)
    ext._messageBox(
        'Embody -- Uninstall Complete',
        f'Removed {summary.get("deleted", 0)} item(s), stripped '
        f'{summary.get("stripped", 0)} shared file(s), un-set '
        f'{summary.get("unset", 0)} git key(s), kept '
        f'{summary.get("kept_review", 0)} flagged item(s).\n\n'
        "Embody's project footprint has been removed. Delete the Embody COMP "
        'to finish removing it from this .toe.',
        buttons=['OK'])
    return summary


# ==========================================================================
# RELEASE .toe EXPORT (C5b)
# ==========================================================================
# ExportPortableTox for the WHOLE project, one-way: tracked files inlined,
# Embody's in-network footprint scrubbed, the Embody COMP + tracking table
# destroyed, optional privacy, save, quit. Dedicated instance; the dev
# .toe and its save series are refused, and from step 0 nothing Embody
# does reaches the project folder (_release_quiet_session). PURE planners
# (plan_release_*), ONE orchestrator; steps 4-6 run from a generated run()
# script because this module is a DAT inside the COMP they delete.

RELEASE_TOE_HOOK = 'pre_release_toe'
# The startup restores land at frames 45-90 (execute.py onStart); a project
# opened seconds ago still holds stripped shells. 120 is those 90 plus margin.
# CAVEAT, stated rather than buried: absTime.frame counts frames since the
# APPLICATION started, not since this project opened, so in an instance that
# already had another project open it passes trivially. That is fine for the
# only supported use (a dedicated instance launched on the project) and it is
# why this is the belt, not the braces -- the per-COMP children census is the
# direct evidence the restores actually ran.
RELEASE_READY_FRAME = 120
# Frames between the last live mutation and the destroy/save tail. Clearing
# file/syncfile on a tracked extension DAT reinitializes that extension, so
# the calling frame must be fully unwound before Embody is destroyed.
RELEASE_FINISH_DELAY_FRAMES = 10
_RELEASE_CLONE_TAG = 'clone'


def release_product_path(embody_path: str) -> str:
    """The COMP a release hook belongs to: Embody's TOP-LEVEL ancestor.

    Not ext.my.parent() -- Embody is installed inside the product it
    externalizes (moonshine: /moonshine/lib/Embody, table sibling
    /moonshine/lib/externalizations), so the parent is a library shelf,
    not the thing being released. Root itself when Embody is a direct
    child of '/'.
    """
    parts = [p for p in str(embody_path or '').split('/') if p]
    return '/' + parts[0] if len(parts) > 1 else '/'


def plan_release_readiness(*, tdxn_comps, frame, op_errors, perform_mode,
                           project_saved, save_path, project_toe='',
                           project_folder='', save_dir_exists=None,
                           save_path_exists=False, hook_touched=(),
                           startup_done=False, ignore_op_errors=False,
                           min_frame=RELEASE_READY_FRAME) -> dict:
    """Every reason to refuse, computed from plain data. PURE.

    tdxn_comps: [{'path', 'present', 'children', 'file_ops'}] -- the
    table's TDXN rows against the live network plus the operator count in
    each row's .tdxn. A childless COMP is a stripped shell (the frame
    45-90 restores have not run; exporting ships it hollow) only when its
    file has operators; empty on disk too it is simply empty; unreadable
    file -> refused, nothing vouches for it. save_path is RESOLVED
    (release_resolve_save_path); save_dir_exists / save_path_exists are
    the caller's os.path checks; hook_touched: COMPs the hook destroyed or
    emptied on purpose (neither 'missing' nor shells); ignore_op_errors
    moves the error refusal to 'warnings'; startup_done waives only the
    frame check.
    """
    refusals, warnings = [], []
    if perform_mode:
        refusals.append('Perform Mode is active')
    if not project_saved:
        refusals.append('project has never been saved -- tracked paths are '
                        'relative to project.folder')
    path = str(save_path or '').strip()
    toe_name = posixpath.basename(str(project_toe or '').replace('\\', '/'))
    if not path:
        refusals.append('a save path is required')
    elif not path.lower().endswith('.toe'):
        refusals.append(f'save path must end in .toe: {path}')
    elif project_toe and _release_same_path(path, project_toe):
        refusals.append(f'save path is the running project itself: {path}')
    elif _release_same_series(path, project_toe, project_folder):
        refusals.append(
            f"save path is in the running project's own folder and save "
            f'series ({posixpath.basename(path)} beside {toe_name}) -- '
            f'TouchDesigner and Embody would take it for the newest save of '
            f'this project; write the release outside the project folder or '
            f'under another base name')
    elif save_dir_exists is False:
        refusals.append(
            f'save folder does not exist: {posixpath.dirname(path)}')
    elif save_path_exists:
        refusals.append(
            f'save path already exists: {path} -- TouchDesigner would ask '
            f'to overwrite it in a session with nothing left to answer; '
            f'delete it or pick another name')
    touched = set(hook_touched or ())
    missing = sorted(c['path'] for c in tdxn_comps
                     if not c.get('present') and c['path'] not in touched)
    shells, unverifiable = [], []
    for c in tdxn_comps:
        if (not c.get('present') or c.get('children')
                or c['path'] in touched):
            continue
        file_ops = c.get('file_ops')
        if file_ops is None:
            unverifiable.append(c['path'])
        elif int(file_ops) > 0:
            shells.append(c['path'])
        # file_ops == 0: empty on disk as well -- legitimately empty.
    shells.sort()
    unverifiable.sort()
    if missing:
        refusals.append(
            f'{len(missing)} tracked COMP(s) are not in the network: '
            + ', '.join(missing))
    if shells:
        refusals.append(
            f'{len(shells)} tracked COMP(s) are stripped shells (empty in '
            f'the network, populated in their .tdxn -- the startup restore '
            f'has not run): ' + ', '.join(shells))
    if unverifiable:
        refusals.append(
            f'{len(unverifiable)} tracked COMP(s) are empty and their .tdxn '
            f'could not be read to confirm that is intentional: '
            + ', '.join(unverifiable))
    if not startup_done and int(frame) <= int(min_frame):
        refusals.append(
            f'frame {int(frame)} is inside the startup window (<= '
            f'{int(min_frame)}) -- the restores land at frames 45-90')
    if op_errors:
        line = (f'the project has {len(op_errors)} operator error(s): '
                + '; '.join(list(op_errors)[:5]))
        (warnings if ignore_op_errors else refusals).append(line)
    return {'ready': not refusals, 'refusals': refusals, 'warnings': warnings,
            'missing': missing, 'shells': shells,
            'unverifiable': unverifiable, 'frame': int(frame),
            'save_path': path}


def _release_same_path(a, b) -> bool:
    """Case- and separator-insensitive path comparison (Windows dev box,
    posix CI). Never resolves -- these are compared, not opened."""
    def _norm(v):
        return str(v or '').replace('\\', '/').rstrip('/').lower()
    return bool(_norm(a)) and _norm(a) == _norm(b)


def release_resolve_save_path(save_path, project_folder) -> str:
    """Absolute, forward-slash form of a save path. PURE.

    A relative path is taken against project.folder: TD runs with that
    folder as its cwd, and every tracked path is relative to it. Resolved
    ONCE, up front, so the gate, the hook's args[0] and the finish script
    all name the same file -- a bare 'MyShow.42.toe' used to slip past the
    running-project refusal and overwrite the dev .toe.
    """
    path = str(save_path or '').strip().replace('\\', '/')
    if not path:
        return ''
    folder = str(project_folder or '').replace('\\', '/').rstrip('/')
    is_abs = path.startswith('/') or (len(path) > 1 and path[1] == ':')
    if not is_abs and folder:
        path = folder + '/' + path
    return posixpath.normpath(path)


def _release_series_base(name) -> str:
    """'MyShow.42.toe' -> 'myshow': the increment series TD's save and
    EmbodyExt._resolveProjectToe group a project's files by."""
    stem = name[:-4] if name.lower().endswith('.toe') else name
    return re.sub(r'\.\d+$', '', stem).lower()


def _release_same_series(save_path, project_toe, project_folder) -> bool:
    """True when the save would land in the project folder under the
    running project's own series -- MyShow.toe beside MyShow.42.toe.
    Neither TD's incremental save nor _resolveProjectToe can tell that
    file from the project's next save."""
    if not (save_path and project_toe and project_folder):
        return False
    path = str(save_path).replace('\\', '/')
    folder = str(project_folder).replace('\\', '/').rstrip('/')
    if posixpath.dirname(path).lower() != folder.lower():
        return False
    toe = posixpath.basename(str(project_toe).replace('\\', '/'))
    return (_release_series_base(posixpath.basename(path))
            == _release_series_base(toe))


def release_storage_keys(skip_keys, control_keys) -> list:
    """The storage keys the scrub may take off ANY operator. PURE.

    Embody's own breadcrumbs by prefix (_tdn_*, _pending_tdn_*,
    _pending_tox_*) plus the Embed-toggle control keys it stores on
    tracked COMPs. The rest of TDXNExt.SKIP_STORAGE_KEYS ('hover',
    'test_results', 'git_status', ...) are Embody-UI runtime names a
    user's own component could carry too; they live on Embody's ops,
    which are destroyed, and are never scrubbed.
    """
    owned = {k for k in (skip_keys or ())
             if k.startswith(('_tdn', '_pending_tdn', '_pending_tox'))}
    return sorted(owned | set(control_keys or ()))


def plan_release_disarm(tdxn_mode, strip_on_save, filecleanup='keep') -> list:
    """The parameter writes that stand Embody's disk writers down. PURE.

    Tdxnmode off: onProjectPreSave short-circuits (execute.py:296-301) and
    checkpoint() refuses -- no .tdxn export, no strip, no tsv restamp for
    the rest of the session. Tdxnstriponsave off: belt and braces for the
    strip (execute.py:395-399). Filecleanup keep: the continuity sweep's
    cascade can never unlink a source file, even if suppression lapses.
    """
    writes = []
    # Filecleanup FIRST: if a later write fails and the session is left
    # standing, 'delete' must already be gone.
    if str(filecleanup or 'keep') != 'keep':
        writes.append(('Filecleanup', 'keep'))
    if str(tdxn_mode or '') != 'off':
        writes.append(('Tdxnmode', 'off'))
    if strip_on_save:
        writes.append(('Tdxnstriponsave', False))
    return writes


def plan_release_presave_hooks(execute_dats, embody_path) -> list:
    """Execute DATs that would still fire onProjectPreSave at the release
    save. PURE.

    Anything inside the Embody COMP is omitted: step 4 destroys it before
    the save, hook and all. What is left belongs to the product, and a
    product pre-save hook running against a project whose Embody is gone
    is exactly the half-initialized state this export exists to avoid --
    so the caller refuses rather than silently disabling someone's DAT.
    Fix it in the pre_release_toe hook (destroy it, or turn its
    projectpresave off), which runs before this check.
    """
    prefix = str(embody_path or '').rstrip('/') + '/'
    out = []
    for d in execute_dats:
        path = d.get('path', '')
        if path == embody_path or path.startswith(prefix):
            continue
        if d.get('projectpresave') and d.get('active', True):
            out.append(path)
    return sorted(out)


def plan_release_inline(tracked, *, embody_path, table_path,
                        hook_path=None, extra_tables=()) -> dict:
    """Which tracked operators lose their file bindings, and which do not.
    PURE. tracked: [{'path', 'family', 'file', 'syncfile', 'externaltox',
    'enableexternaltox'}] -- the live state of every table row.

    Skipped, all load-bearing: the tracking table(s) -- a table DAT keeps
    its TEXT when its file is cleared, i.e. the whole source manifest
    (destroyed in step 4 instead); the pre_release_toe hook (destroyed in
    step 2, it holds the recipe); everything inside the Embody COMP
    (destroyed whole, and clearing EmbodyExt.py's binding reinits THIS
    extension mid-call).
    """
    prefix = str(embody_path or '').rstrip('/') + '/'
    tables = {t for t in [table_path, *(extra_tables or ())] if t}
    plan = {'dats': [], 'comps': [], 'skipped': []}

    def _skip(path, why):
        plan['skipped'].append({'path': path, 'why': why})

    for rec in tracked:
        path = rec.get('path', '')
        if path == embody_path or path.startswith(prefix):
            _skip(path, 'inside the Embody COMP (destroyed in step 4)')
            continue
        if path in tables:
            _skip(path, 'the externalizations table (destroyed in step 4 -- '
                        'inlining it would ship the file manifest)')
            continue
        if hook_path and path == hook_path:
            _skip(path, 'the release hook (destroyed in step 2)')
            continue
        if rec.get('family') == 'DAT':
            if rec.get('file') or rec.get('syncfile'):
                plan['dats'].append(path)
            else:
                _skip(path, 'no file binding')
        elif rec.get('family') == 'COMP':
            if rec.get('externaltox') or rec.get('enableexternaltox'):
                plan['comps'].append(path)
            else:
                _skip(path, 'no external .tox binding')
        else:
            _skip(path, f'unsupported family {rec.get("family")!r}')
    plan['dats'].sort()
    plan['comps'].sort()
    return plan


def plan_release_reference_sweep(refs, inlined=()) -> dict:
    """Bindings still standing after the inline pass. PURE.

    refs: [{'path', 'par', 'value'}]; inlined: paths the inline plan
    clears, skipped so a preview shows what WOULD remain. Absolute values
    are called out separately -- a relative one is merely a dangling
    reference in the artifact, an absolute one leaks the build machine's
    filesystem. /sys/ paths are TouchDesigner's own and are neither.
    """
    skip = set(inlined or ())
    remaining, absolute = [], []
    for ref in refs:
        value = str(ref.get('value') or '')
        if not value or ref.get('path', '') in skip:
            continue
        entry = {'path': ref.get('path', ''), 'par': ref.get('par', ''),
                 'value': value}
        remaining.append(entry)
        if value.startswith('/sys/'):
            continue
        if value.startswith('/') or (len(value) > 1 and value[1] == ':'):
            absolute.append(entry)
    return {'remaining': remaining, 'absolute': absolute}


def release_tag_removals(tags, embody_tags, exclude_tags) -> list:
    """Embody tags to strip off one operator. PURE.

    Covers BOTH spellings of the boundary and exclude tags (the caller
    unions the configured values with the legacy ones -- EmbodyExt._tdxnTags
    / ._tdxnExcludeTags), the 'clone' marker Embody adds itself
    (EmbodyExt.py:8968), and the per-parameter QUALIFIERS the exclude tag
    takes: 'tdxn_exclude:play' is one tag, not two, and a plain membership
    test never sees it.
    """
    drop = set()
    for tag in tags:
        if tag in embody_tags or tag in exclude_tags:
            drop.add(tag)
        elif tag == _RELEASE_CLONE_TAG:
            drop.add(tag)
        elif ':' in tag and tag.split(':', 1)[0] in exclude_tags:
            drop.add(tag)
    return sorted(drop)


def plan_release_scrub(ops, *, embody_tags, exclude_tags, storage_keys,
                       tracked_paths=(), embody_path='', table_path='',
                       extra_tables=()) -> dict:
    """Embody's IN-NETWORK footprint, per operator. PURE.

    No manifest exists for it (_INSTALL_MANIFEST is files on disk), so it
    is derived: tags (release_tag_removals), node colours (EmbodyExt.
    Disable's rule: every tagged op, plus tracked ops whose tag was lost)
    and storage_keys (release_storage_keys: Embody-owned names only, off
    any op). The Embody COMP, its subtree and the table(s) are left alone,
    destroyed whole in step 4.
    """
    tracked = set(tracked_paths or ())
    embody_path = str(embody_path or '')
    prefix = embody_path.rstrip('/') + '/'
    tables = {t for t in [table_path, *(extra_tables or ())] if t}
    tag_plan, storage_plan, colours = [], [], set()
    keys = set(storage_keys or ())
    for rec in ops:
        path = rec.get('path', '')
        if embody_path and (path == embody_path or path.startswith(prefix)):
            continue
        if path in tables:
            continue
        removals = release_tag_removals(
            rec.get('tags') or (), embody_tags, exclude_tags)
        if removals:
            tag_plan.append({'path': path, 'remove': removals})
            colours.add(path)
        if path in tracked:
            colours.add(path)
        hit = sorted(keys.intersection(rec.get('storage_keys') or ()))
        if hit:
            storage_plan.append({'path': path, 'keys': hit})
    tag_plan.sort(key=lambda e: e['path'])
    storage_plan.sort(key=lambda e: e['path'])
    return {'tags': tag_plan, 'storage': storage_plan,
            'colours': sorted(colours)}


def plan_release_footprint(embody_path, table_path, extra_tables=()) -> list:
    """What step 4 destroys, in order. PURE.

    The Embody COMP FIRST, its tracking table second: the table is
    deliberately an undocked sibling so it survives the COMP's deletion
    (that is what makes an accidental delete recoverable), which is
    exactly why the release has to name it separately. A table INSIDE the
    COMP goes with it and is dropped rather than destroyed twice.
    extra_tables: sibling 'externalizations' tables the par no longer
    links (createExternalizationsTable re-adopts one by name) -- their
    text is the manifest all the same.
    """
    embody_path = str(embody_path or '')
    prefix = embody_path.rstrip('/') + '/'
    plan = [{'path': embody_path, 'what': 'Embody COMP'}]
    for path, what in [(table_path, 'externalizations table')] + [
            (t, 'externalizations table (unlinked sibling)')
            for t in (extra_tables or ())]:
        path = str(path or '')
        if (not path or path == embody_path or path.startswith(prefix)
                or any(e['path'] == path for e in plan)):
            continue
        plan.append({'path': path, 'what': what})
    return plan


def plan_release_privacy(privacy_key, is_pro) -> dict:
    """Project privacy, and the licence that gates it. PURE.

    project.addPrivacy needs Pro (docs.derivative.ca/Project_Class) and
    only ever applies to a .toe that has none. Refusing here rather than
    at the call site matters: by then Embody is deleted and there is
    nothing left to report with.
    """
    key = str(privacy_key or '')
    if not key:
        return {'apply': False, 'key': '', 'refusal': ''}
    if not is_pro:
        return {'apply': False, 'key': key,
                'refusal': 'project privacy needs a Pro licence '
                           '(licenses.isPro is False) -- run the release on '
                           'a Pro machine or export without a privacy key'}
    return {'apply': True, 'key': key, 'refusal': ''}


def plan_release_toe(state, *, save_path, privacy_key=None,
                     hook_name=RELEASE_TOE_HOOK, ignore_op_errors=False,
                     hook_touched=(), hook_ran=False) -> dict:
    """Compose the whole plan from collected state. PURE -- `state` is not
    mutated, nothing is applied, and this is what the dry run returns.

    `state` is what _release_collect reads out of TouchDesigner; see it for
    the shape of each key.
    """
    embody_path = state.get('embody_path', '')
    table_path = state.get('table_path', '')
    hook_path = state.get('hook_path') or None
    extra_tables = tuple(state.get('orphan_tables') or ())
    resolved = release_resolve_save_path(save_path,
                                         state.get('project_folder', ''))
    readiness = plan_release_readiness(
        tdxn_comps=state.get('tdxn_comps') or (),
        frame=state.get('frame', 0),
        op_errors=state.get('op_errors') or (),
        perform_mode=state.get('perform_mode', False),
        project_saved=state.get('project_saved', False),
        save_path=resolved,
        project_toe=state.get('project_toe', ''),
        project_folder=state.get('project_folder', ''),
        save_dir_exists=state.get('save_dir_exists'),
        save_path_exists=bool(state.get('save_path_exists')),
        hook_touched=hook_touched,
        startup_done=state.get('startup_done', False),
        ignore_op_errors=ignore_op_errors)
    privacy = plan_release_privacy(privacy_key, state.get('is_pro', False))
    if privacy['refusal']:
        readiness['refusals'] = list(readiness['refusals']) + [
            privacy['refusal']]
        readiness['ready'] = False
    presave = plan_release_presave_hooks(state.get('execute_dats') or (),
                                         embody_path)
    if presave and not hook_path and not hook_ran:
        # Nothing between the gate and the save could disarm them. After
        # the hook ran (and was destroyed) the caller refuses on its own.
        readiness['refusals'] = list(readiness['refusals']) + [
            f'{len(presave)} Execute DAT(s) still fire onProjectPreSave and '
            f'there is no {hook_name} hook to disarm them: '
            + ', '.join(presave)]
        readiness['ready'] = False
    inline = plan_release_inline(
        state.get('tracked') or (), embody_path=embody_path,
        table_path=table_path, hook_path=hook_path,
        extra_tables=extra_tables)
    return {
        'save_path': resolved,
        'embody_path': embody_path,
        'table_path': table_path,
        'product_path': (state.get('product_path')
                         or release_product_path(embody_path)),
        'hook_name': hook_name,
        'hook_path': hook_path or '',
        'readiness': readiness,
        'disarm': plan_release_disarm(state.get('tdxn_mode', ''),
                                      state.get('strip_on_save', False),
                                      state.get('filecleanup', 'keep')),
        'presave_hooks': presave,
        'inline': inline,
        'references': plan_release_reference_sweep(
            state.get('refs') or (),
            inlined=set(inline['dats']) | set(inline['comps'])),
        'scrub': plan_release_scrub(
            state.get('ops') or (),
            embody_tags=state.get('embody_tags') or (),
            exclude_tags=state.get('exclude_tags') or (),
            storage_keys=state.get('storage_keys') or (),
            tracked_paths=state.get('tracked_paths') or (),
            embody_path=embody_path, table_path=table_path,
            extra_tables=extra_tables),
        'footprint': plan_release_footprint(embody_path, table_path,
                                            extra_tables),
        'privacy': privacy,
    }


def release_finish_script(footprint, save_path, privacy_key='',
                          quit_after=True) -> str:
    """Steps 4-6 as a run() script. PURE (a string in, a string out).

    It runs with NO Embody: this module, the extension and the log all
    live in the COMP its first act destroys, so it interpolates literals,
    reaches only TD globals, and reports through print(). It is a script
    rather than a Text DAT at '/' because an operator holding the release
    recipe would have to be destroyed before the save to avoid shipping --
    a run() string cannot be saved into a .toe at all.

    Fail-CLOSED: a target that survives, a refused addPrivacy or a failed
    save all stop before the next step and leave the session standing for
    inspection. Nothing is ever written half-locked.
    """
    lines = [
        '# Embody ExportReleaseToe -- generated, runs after the Embody COMP is gone',
        '_targets = %r' % ([e['path'] for e in footprint],),
        '_path = %r' % (str(save_path),),
        '_key = %r' % (str(privacy_key or ''),),
        '_quit = %r' % (bool(quit_after),),
        'for _p in _targets:',
        '    _o = op(_p)',
        '    if _o is not None:',
        '        _o.destroy()',
        '_left = [_p for _p in _targets if op(_p) is not None]',
        'if _left:',
        "    print('Embody > ExportReleaseToe ABORTED: still present: %s' % _left)",
        'elif _key and not project.addPrivacy(_key):',
        "    print('Embody > ExportReleaseToe ABORTED: addPrivacy refused "
        "(Pro licence? already private?) -- nothing was saved')",
        'elif not project.save(_path):',
        "    print('Embody > ExportReleaseToe ABORTED: project.save failed: %s' % _path)",
        'else:',
        "    print('Embody > ExportReleaseToe: saved %s' % _path)",
        '    if _quit:',
        '        project.quit(force=True)',
    ]
    return '\n'.join(lines)


# ---- orchestrator (reads TD, calls the planners, applies the plan) --------

def _release_collect(ext, hook_name=RELEASE_TOE_HOOK, save_path=None) -> dict:
    """Read everything the planners need out of TouchDesigner, as plain
    data. The ONLY function here that touches the live network to decide
    anything -- keeping it in one place is what makes the plan testable.

    startup_done is always False: 6.2.41 has no restores-done flag (init()
    stores _init_complete at onStart, long BEFORE the frame-45..90
    restores, so reading it would defeat the gate). The frame ceiling plus
    the per-COMP children census is the evidence instead.
    """
    embody = ext.my
    embody_path = embody.path
    table = ext.Externalizations
    table_path = table.path if table else ''
    product = op(release_product_path(embody_path))
    if product is None:
        product = ext.root

    tdxn_comps = []
    for comp_path, rel in ext._getTDXNStrategyComps():
        comp = op(comp_path)
        tdxn_comps.append({
            'path': comp_path,
            'present': comp is not None,
            'children': (len(comp.findChildren(depth=1, includeUtility=True))
                         if comp is not None else 0),
            'file_ops': _release_tdxn_file_ops(ext, rel)})

    tracked, tracked_paths = [], []
    if table:
        for i in range(1, table.numRows):
            path = ext._cellVal(i, 'path', table=table)
            # resolveOpIncludingUtility, not op(): a legacy row AT an
            # annotate is invisible to a bare lookup (EmbodyExt:9830).
            oper = ext.resolveOpIncludingUtility(path) if path else None
            if oper is None:
                continue
            tracked_paths.append(path)
            tracked.append({
                'path': path,
                'family': oper.family,
                'file': _release_par(oper, 'file'),
                'syncfile': bool(_release_par(oper, 'syncfile')),
                'externaltox': _release_par(oper, 'externaltox'),
                'enableexternaltox': bool(
                    _release_par(oper, 'enableexternaltox'))})

    ops = _release_ops_census(ext)

    # A sibling table the Externalizations par no longer links: Embody
    # re-adopts one by name (createExternalizationsTable), and its text
    # is the manifest all the same.
    orphan_tables = []
    try:
        for sib in embody.parent().findChildren(depth=1,
                                                name='externalizations'):
            if sib.family == 'DAT' and sib.path != table_path:
                orphan_tables.append(sib.path)
    except Exception:
        pass

    hook = ext._findReleaseHook(product, hook_name)
    folder = str(project.folder or '')
    dir_exists, path_exists = _release_save_path_state(save_path, folder)
    return {
        'embody_path': embody_path,
        'table_path': table_path,
        'product_path': product.path,
        'hook_path': hook.path if hook is not None else '',
        'tdxn_comps': tdxn_comps,
        'tracked': tracked,
        'tracked_paths': tracked_paths,
        'ops': ops,
        'orphan_tables': orphan_tables,
        'refs': _release_remaining_refs(ext),
        'execute_dats': _release_execute_dats(ext),
        'op_errors': _release_op_errors(ext),
        'frame': _release_frame(),
        'startup_done': False,
        'perform_mode': bool(ext._performMode),
        'project_saved': bool(ext._projectSavedOnDisk()),
        'project_toe': ext._resolveProjectToe() or '',
        'project_folder': folder,
        'save_dir_exists': dir_exists,
        'save_path_exists': path_exists,
        'tdxn_mode': ext._tdxnMode(),
        'strip_on_save': bool(_release_par(embody, 'Tdxnstriponsave',
                                           False)),
        'filecleanup': str(_release_par(embody, 'Filecleanup', 'keep')
                           or 'keep'),
        'embody_tags': sorted(set(ext.getTags()) | set(ext._tdxnTags())),
        'exclude_tags': sorted(ext._tdxnExcludeTags()),
        'storage_keys': release_storage_keys(
            ext._storageSkipKeys(),
            getattr(ext, '_STORAGE_CONTROL_KEYS', ())),
        'is_pro': _release_is_pro(),
    }


def _release_ops_census(ext) -> list:
    """Tags and storage keys of every operator, root included."""
    return [{'path': oper.path, 'tags': list(oper.tags),
             'storage_keys': list(oper.storage.keys())}
            for oper in [ext.root] + ext.root.findChildren(includeUtility=True)]


def _release_tdxn_file_ops(ext, rel_path):
    """Operator count in a tracked COMP's .tdxn on disk, None when it cannot
    be read. What tells a stripped shell (empty live, populated on disk)
    from a COMP that is simply empty."""
    try:
        doc = ext.my.ext.TDXN._read_existing_tdxn(
            str(ext.buildAbsolutePath(rel_path)))
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    ops = doc.get('operators')
    return len(ops) if isinstance(ops, (list, dict)) else 0


def _release_save_path_state(save_path, project_folder):
    """(folder exists, file exists) for the resolved save path; (None,
    False) without a path so 'a save path is required' stays the only
    word."""
    resolved = release_resolve_save_path(save_path, project_folder)
    if not resolved:
        return None, False
    return (os.path.isdir(posixpath.dirname(resolved)),
            os.path.exists(resolved))


def _release_par(oper, name, default=''):
    """Evaluate a parameter that may not exist on this operator family."""
    par = getattr(oper.par, name, None)
    if par is None:
        return default
    try:
        return par.eval()
    except Exception:
        return default


def _release_execute_dats(ext) -> list:
    """Every Execute DAT in the project with its pre-save arming.

    Only the plain Execute DAT carries projectpresave -- the parameter /
    dat / chop / panel exec families do not -- so the type filter is the
    whole population, not an optimization.
    """
    return [{'path': d.path,
             'projectpresave': bool(_release_par(d, 'projectpresave')),
             'active': bool(_release_par(d, 'active', default=1))}
            for d in ext.root.findChildren(type=DAT)
            if getattr(d, 'type', '') == 'execute'
            and not d.path.startswith(('/local/', '/sys/', '/ui/'))]


def _release_frame():
    """Frames since the APPLICATION started -- see RELEASE_READY_FRAME."""
    try:
        return int(absTime.frame)
    except Exception:
        return 0


def _release_is_pro():
    try:
        return bool(licenses.isPro)
    except Exception:
        return False


def _release_op_errors(ext) -> list:
    """Project-wide operator + script errors, the shape
    EmbodyExt._verifyReconstructedComp reads them in (:12420-12445).
    Capped: the gate needs to know THAT the project is broken. Embody's own
    subtree (destroyed) and /local (never saved) are not the project's."""
    errors = []
    embody_path = ext.my.path
    prefix = embody_path + '/'
    try:
        for child in ext.root.findChildren():
            path = child.path
            if (path == embody_path or path.startswith(prefix)
                    or path in ('/local', '/sys', '/ui')
                    or path.startswith(('/local/', '/sys/', '/ui/'))):
                continue
            for text in (child.scriptErrors(), child.errors()):
                if text:
                    errors.append(f'{child.path}: {text.strip()}')
            if len(errors) >= 50:
                break
    except Exception as e:
        ext.Log(f'Release gate: operator error scan failed: {e}', 'WARNING')
    return errors


def preview_release_toe(ext, save_path=None, privacy_key=None,
                        hook_name=RELEASE_TOE_HOOK,
                        ignore_op_errors=False) -> dict:
    """Log + return a NON-DESTRUCTIVE preview of a full ExportReleaseToe.
    Nothing is run, nothing is disarmed, nothing is destroyed."""
    plan = plan_release_toe(_release_collect(ext, hook_name, save_path),
                            save_path=save_path, privacy_key=privacy_key,
                            hook_name=hook_name,
                            ignore_op_errors=ignore_op_errors)
    ready = plan['readiness']
    lines = [f'Release .toe preview -> {plan["save_path"] or "(no path)"}']
    if not ready['ready']:
        lines.append(f'  REFUSED ({len(ready["refusals"])}):')
        lines.extend(f'    x {r}' for r in ready['refusals'])
    lines.extend(f'  WARNING: {w}' for w in ready['warnings'])
    lines.append(f'  hook: {plan["hook_path"] or "none"} (looked for '
                 f'{plan["hook_name"]} under {plan["product_path"]}) -- the '
                 f'export re-plans everything below AFTER it runs')
    lines.append(f'  inline: {len(plan["inline"]["dats"])} DAT(s), '
                 f'{len(plan["inline"]["comps"])} COMP(s), '
                 f'{len(plan["inline"]["skipped"])} skipped')
    lines.append(f'  scrub: {len(plan["scrub"]["tags"])} tagged, '
                 f'{len(plan["scrub"]["colours"])} coloured, '
                 f'{len(plan["scrub"]["storage"])} with storage')
    lines.append('  destroy: '
                 + ', '.join(f'{e["path"]} ({e["what"]})'
                             for e in plan['footprint']))
    if plan['presave_hooks']:
        lines.append('  BLOCKING pre-save hooks: '
                     + ', '.join(plan['presave_hooks']))
    refs = plan['references']
    lines.append(f'  references left after inlining: '
                 f'{len(refs["remaining"])} ({len(refs["absolute"])} '
                 f'absolute)')
    absolute = {(e['path'], e['par']) for e in refs['absolute']}
    lines.extend(f'    ! {e["path"]}.{e["par"]} = {e["value"]}'
                 for e in refs['absolute'][:10])
    lines.extend(f'    - {e["path"]}.{e["par"]} = {e["value"]}'
                 for e in [r for r in refs['remaining']
                           if (r['path'], r['par']) not in absolute][:10])
    lines.append('  privacy: '
                 + ('yes' if plan['privacy']['apply'] else 'no'))
    ext.Log('\n'.join(lines), 'INFO')
    return plan


def export_release_toe(ext, save_path, privacy_key=None,
                       hook_name=RELEASE_TOE_HOOK, quit_after=True,
                       confirm=False, ignore_op_errors=False) -> dict:
    """Export the project as a locked, self-contained release .toe.

    DESTRUCTIVE and one-way -- requires confirm=True (review
    PreviewReleaseToe() first). Quiets the session, inlines every tracked
    externalization, runs the pre_release_toe hook, re-plans, scrubs
    Embody's in-network footprint, then hands steps 4-6 (destroy Embody +
    the table, privacy, save, quit) to a generated run() script because
    this module lives inside the COMP those steps delete. The live session
    does not survive; the dev .toe on disk is never written.
    """
    if not confirm:
        ext.Log('ExportReleaseToe is destructive and one-way. Review '
                'PreviewReleaseToe() first, then call '
                'ExportReleaseToe(save_path, confirm=True). Nothing was '
                'changed.', 'WARNING')
        return {'ran': False, 'reason': 'confirm required'}

    state = _release_collect(ext, hook_name, save_path)
    plan = plan_release_toe(state, save_path=save_path,
                            privacy_key=privacy_key, hook_name=hook_name,
                            ignore_op_errors=ignore_op_errors)
    if not plan['readiness']['ready']:
        for reason in plan['readiness']['refusals']:
            ext.Log(f'ExportReleaseToe refused: {reason}', 'ERROR')
        return {'ran': False, 'reason': 'not ready',
                'refusals': plan['readiness']['refusals'], 'plan': plan}
    for line in plan['readiness']['warnings']:
        ext.Log(f'ExportReleaseToe: {line}', 'WARNING')

    # Step 0: nothing Embody does from here on may reach the project
    # folder or block on a modal.
    failed = _release_quiet_session(ext, plan['disarm'])
    if failed:
        ext.Log(f'ExportReleaseToe aborted: could not quiet the session '
                f'({failed}) -- nothing was inlined or destroyed', 'ERROR')
        return {'ran': False, 'reason': f'could not quiet the session: {failed}'}

    # Step 1: inline every tracked externalization (EmbodyExt.Disable's
    # strip half, :3606-3631 -- with no restore, because nothing returns).
    # BEFORE the hook: a synced DAT the hook stamps would otherwise write
    # straight through to the source file on disk.
    inlined = _apply_release_inline(ext, plan['inline'])
    if inlined['errors']:
        ext.Log(f'ExportReleaseToe aborted: {inlined["errors"]} tracked '
                f'operator(s) kept their file binding (see the warnings '
                f'above) -- nothing was destroyed', 'ERROR')
        return {'ran': False, 'reason': 'inline failed', 'inlined': inlined}

    # Step 2: the author's hook, then destroy it -- it holds the recipe.
    populated_before = {c['path'] for c in state['tdxn_comps']
                        if c.get('present') and c.get('children')}
    toe_before = _release_project_toe_state(ext)
    product = op(plan['product_path'])
    if plan['hook_path'] and product is not None:
        version = str(ext.my.par.Version.eval())
        # (found, ok) -- found is already known from the plan.
        if not ext._runReleaseHook(
                product, hook_name, (plan['save_path'], version))[1]:
            ext.Log(f'ExportReleaseToe aborted: {hook_name} hook failed -- '
                    f'the hook DAT is KEPT for inspection', 'ERROR')
            return {'ran': False, 'reason': 'hook failed'}
        if not getattr(ext.my, 'valid', True):
            # The hook took Embody with it: no logger, no table, no tail.
            return {'ran': False, 'reason': 'hook destroyed Embody'}
        hook = op(plan['hook_path'])
        if hook is not None:
            hook.destroy()
            ext.Log(f'ExportReleaseToe: destroyed {plan["hook_path"]}',
                    'INFO')
        if _release_project_toe_state(ext) != toe_before:
            ext.Log(f'ExportReleaseToe aborted: the {hook_name} hook saved '
                    f'the project -- the export never saves your project; '
                    f'take the project.save() out of the hook', 'ERROR')
            return {'ran': False, 'reason': 'hook saved the project'}
    else:
        ext.Log(f'ExportReleaseToe: no {hook_name} hook under '
                f'{plan["product_path"]}', 'INFO')

    # RE-PLAN against the post-hook network (ExportPortableTox runs
    # pre_release before collection for the same reason). The gate is
    # re-run too: a hook that breaks the project stops the release. A
    # tracked COMP the hook destroyed or emptied keeps its row and is
    # neither 'missing' nor a shell.
    hook_ran = bool(plan['hook_path'])
    state = _release_collect(ext, hook_name, save_path)
    touched = sorted(populated_before - {
        c['path'] for c in state['tdxn_comps']
        if c.get('present') and c.get('children')})
    plan = plan_release_toe(state, save_path=save_path,
                            privacy_key=privacy_key, hook_name=hook_name,
                            ignore_op_errors=ignore_op_errors,
                            hook_touched=touched, hook_ran=hook_ran)
    if not plan['readiness']['ready']:
        for reason in plan['readiness']['refusals']:
            ext.Log(f'ExportReleaseToe aborted after the {hook_name} hook: '
                    f'{reason}', 'ERROR')
        return {'ran': False, 'reason': 'not ready after the hook',
                'refusals': plan['readiness']['refusals'], 'plan': plan}
    for line in plan['readiness']['warnings']:
        ext.Log(f'ExportReleaseToe (after the hook): {line}', 'WARNING')
    # The hook is the place to deal with a product's own pre-save hook, so
    # this check reads the post-hook network and blocks rather than
    # switching someone else's DAT off.
    if plan['presave_hooks']:
        return _release_refuse_presave(ext, plan['presave_hooks'], hook_name)

    sweep = plan_release_reference_sweep(_release_remaining_refs(ext))
    if sweep['remaining']:
        ext.Log(f'ExportReleaseToe: {len(sweep["remaining"])} file/'
                f'externaltox binding(s) Embody never tracked remain: '
                + ', '.join(f'{e["path"]}.{e["par"]}'
                            for e in sweep['remaining'][:10]), 'INFO')
    for entry in sweep['absolute']:
        ext.Log(f'ExportReleaseToe: absolute path still bound -- '
                f'{entry["path"]}.{entry["par"]} = {entry["value"]}',
                'WARNING')

    # Step 3: scrub the in-network footprint. Censused AGAIN here: clearing
    # a tracked extension's source binding can reinit that extension, and
    # what its __init__ stored or tagged is invisible to the gate's plan.
    scrub_plan = plan_release_scrub(
        _release_ops_census(ext),
        embody_tags=state['embody_tags'],
        exclude_tags=state['exclude_tags'],
        storage_keys=state['storage_keys'],
        tracked_paths=state['tracked_paths'],
        embody_path=plan['embody_path'], table_path=plan['table_path'],
        extra_tables=state.get('orphan_tables') or ())
    scrubbed = _apply_release_scrub(ext, scrub_plan)
    if scrubbed['errors']:
        ext.Log(f'ExportReleaseToe aborted: {scrubbed["errors"]} operator(s) '
                f'kept an Embody tag, colour or storage key (see the '
                f'warnings above) -- nothing was destroyed', 'ERROR')
        return {'ran': False, 'reason': 'scrub failed', 'inlined': inlined,
                'scrubbed': scrubbed}
    # Embot and the colour pulses are retired at every save by Embody's
    # pre-save hook, which dies with the COMP: retire them here or they ship.
    _release_retire_viz(ext)

    # Steps 4-6, deferred so this frame unwinds first: clearing file/
    # syncfile above reinitializes every extension whose source DAT it
    # touched, and the COMP destroyed next is the one running this code.
    script = release_finish_script(plan['footprint'], plan['save_path'],
                                   plan['privacy']['key']
                                   if plan['privacy']['apply'] else '',
                                   quit_after)
    ext.Log(f'ExportReleaseToe: inlined {inlined}, scrubbed {scrubbed}; '
            f'destroying Embody and saving {plan["save_path"]} in '
            f'{RELEASE_FINISH_DELAY_FRAMES} frames', 'SUCCESS')
    run(script, delayFrames=RELEASE_FINISH_DELAY_FRAMES)
    return {'ran': True, 'save_path': plan['save_path'],
            'warnings': plan['readiness']['warnings'],
            'touched_by_hook': touched,
            'inlined': inlined, 'scrubbed': scrubbed,
            'remaining_refs': sweep['remaining'],
            'absolute_refs': sweep['absolute'],
            'footprint': plan['footprint'],
            'privacy': plan['privacy']['apply'],
            'quit_after': bool(quit_after), 'plan': plan}


def _release_refuse_presave(ext, blocking, hook_name) -> dict:
    ext.Log('ExportReleaseToe aborted: these execute DATs still fire '
            'onProjectPreSave and would run against a project with no '
            'Embody -- destroy them or turn projectpresave off in the '
            f'{hook_name} hook: ' + ', '.join(blocking), 'ERROR')
    return {'ran': False, 'reason': 'pre-save hooks armed',
            'presave_hooks': list(blocking)}


def _release_project_toe_state(ext):
    """(path, mtime) of the project's newest .toe on disk -- the receipt
    that tells whether a hook saved the project."""
    try:
        path = ext._resolveProjectToe() or ''
        return (path, os.path.getmtime(path) if path else None)
    except Exception:
        return ('', None)


def _release_quiet_session(ext, writes):
    """Step 0: nothing Embody does for the rest of this session may reach
    the project folder or block on a modal. _suppress_dialogs: every
    _messageBox returns its default and checkOpsForContinuity skips the
    file-cleanup cascade (post-inline, every tracked op reads 'replaced').
    _init_complete unstored + _restoring_settings: the save-window guards
    parexec.onValueChange reads when it RUNS -- TD delivers it deferred,
    and parexec.par.active=False only postpones it (measured 2026-09-08).
    Status Disabled: Update() returns at once (a hook, a peer, Ctrl+S or
    Ctrl+Shift+U would otherwise re-arm every TOX binding). Embody's
    execute DAT inactive: no pre/post-save hooks. A chunked TDXN export
    in flight is cancelled (as the pre-save path does): its batches run
    on their own frames and honour no mode. Table syncfile off: no tsv
    sync. Then the disarm writes. Fail-CLOSED: returns what failed (the
    caller aborts), or None."""
    try:
        ext.my.store('_suppress_dialogs', True)
        ext.my.unstore('_init_complete')
        ext._restoring_settings = True
    except Exception as e:
        return f'suppress: {e}'
    try:
        cancel = getattr(ext.my.ext.TDXN, 'cancelExport', None)
        if cancel is not None:
            cancel()
    except Exception as e:
        return f'cancel TDXN export: {e}'
    try:
        ext.my.par.Status = 'Disabled'
        execute = ext.my.op('execute')
        if execute is not None:
            execute.par.active = False
    except Exception as e:
        return f'Update/save hooks: {e}'
    table = ext.Externalizations
    if table is not None:
        try:
            table.par.syncfile = False
        except Exception as e:
            return f'table syncfile: {e}'
    return _apply_release_disarm(ext, writes)


def _apply_release_disarm(ext, writes):
    """Write the disarm plan, every write attempted even after a failure
    (Filecleanup comes first for the same reason). Only after
    _release_quiet_session: with parexec live, Tdxnmode -> off raises the
    Disable-TDXN modal (_onTdxnModeChanged) and, the pars being
    _PERSISTED_PARAMS, rewrites .embody/config.json -- shared with the
    editing instance on the same project folder. Returns the first
    failing par name, or None."""
    failed = None
    for name, value in writes:
        try:
            setattr(ext.my.par, name, value)
            ext.Log(f'ExportReleaseToe: {name} -> {value!r}', 'INFO')
        except Exception as e:
            ext.Log(f'ExportReleaseToe: could not set {name}: {e}', 'ERROR')
            failed = failed or name
    return failed


def _release_retire_viz(ext) -> None:
    """Retire Embot + colour pulses (EnvoyExt viz), as the save path does."""
    envoy = getattr(ext.my.ext, 'Envoy', None)
    if envoy is None:
        return
    try:
        envoy._vizCleanup()
        purged = envoy._purgeVizArtifacts()
        if purged:
            ext.Log(f'ExportReleaseToe: purged {purged} orphaned Embot '
                    f'part(s)', 'WARNING')
    except Exception as e:
        ext.Log(f'ExportReleaseToe: viz cleanup failed: {e}', 'WARNING')


def _apply_release_inline(ext, inline_plan) -> dict:
    """Clear the file bindings the inline plan names. No restore phase --
    this session ends at the save. syncfile first, as EmbodyExt.Disable
    does: a synced DAT never sees its file par change while still armed."""
    done = {'dats': 0, 'comps': 0, 'errors': 0}
    for path in inline_plan['dats']:
        oper = ext.resolveOpIncludingUtility(path)
        if oper is None:
            continue
        try:
            sync = getattr(oper.par, 'syncfile', None)
            if sync is not None:
                oper.par.syncfile = False
            oper.par.file.readOnly = False
            oper.par.file = ''
            done['dats'] += 1
        except Exception as e:
            done['errors'] += 1
            ext.Log(f'ExportReleaseToe: could not inline {path}: {e}',
                    'WARNING')
    for path in inline_plan['comps']:
        oper = ext.resolveOpIncludingUtility(path)
        if oper is None:
            continue
        try:
            oper.par.externaltox.readOnly = False
            oper.par.externaltox = ''
            oper.par.enableexternaltox = False
            done['comps'] += 1
        except Exception as e:
            done['errors'] += 1
            ext.Log(f'ExportReleaseToe: could not inline {path}: {e}',
                    'WARNING')
    return done


def _release_remaining_refs(ext) -> list:
    """Every file / externaltox binding standing outside the Embody COMP
    and its table -- both destroyed, neither shipped."""
    embody_path = ext.my.path
    prefix = embody_path + '/'
    table = ext.Externalizations
    table_path = table.path if table else ''
    refs = []
    for oper in ext.root.findChildren(includeUtility=True):
        path = oper.path
        if path == embody_path or path.startswith(prefix) or path == table_path:
            continue
        for name in ('file', 'externaltox'):
            value = _release_par(oper, name)
            if value:
                refs.append({'path': oper.path, 'par': name,
                             'value': str(value)})
    return refs


def _apply_release_scrub(ext, scrub_plan) -> dict:
    """Remove Embody's tags, colours and storage breadcrumbs."""
    done = {'tags': 0, 'colours': 0, 'storage': 0, 'errors': 0}
    for entry in scrub_plan['tags']:
        oper = ext.resolveOpIncludingUtility(entry['path'])
        if oper is None:
            continue
        try:
            for tag in entry['remove']:
                if tag in oper.tags:
                    oper.tags.remove(tag)
            done['tags'] += 1
        except Exception as e:
            done['errors'] += 1
            ext.Log(f'ExportReleaseToe: could not untag {entry["path"]}: {e}',
                    'WARNING')
    for path in scrub_plan['colours']:
        oper = ext.resolveOpIncludingUtility(path)
        if oper is None:
            continue
        try:
            ext.resetOpColor(oper)
            done['colours'] += 1
        except Exception as e:
            done['errors'] += 1
            ext.Log(f'ExportReleaseToe: could not recolour {path}: {e}',
                    'WARNING')
    for entry in scrub_plan['storage']:
        oper = ext.resolveOpIncludingUtility(entry['path'])
        if oper is None:
            continue
        try:
            for key in entry['keys']:
                oper.unstore(key)
            done['storage'] += 1
        except Exception as e:
            done['errors'] += 1
            ext.Log(f'ExportReleaseToe: could not unstore on '
                    f'{entry["path"]}: {e}', 'WARNING')
    return done


# ==========================================================================
# SETTINGS PERSISTENCE (C9)
# ==========================================================================

def settings_path(ext) -> Path:
    """Path to .embody/config.json -- consistent with _findProjectRoot()."""
    return ext._findProjectRoot() / '.embody' / 'config.json'


def find_settings_file(ext) -> Optional[Path]:
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
    canonical = settings_path(ext)
    if canonical.is_file():
        return canonical
    # Try the alternate predefined modes (gitroot, projectfolder).
    for mode in ('gitroot', 'projectfolder'):
        alt = ext._rootForMode(mode) / '.embody' / 'config.json'
        if alt != canonical and alt.is_file():
            ext.Log(
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
            ext.Log(
                f'config.json found by ancestor walk-up: {candidate}',
                'INFO')
            return candidate
    return None


def project_json_path(ext) -> Path:
    """Path to .embody/project.json -- committed project metadata.

    Unlike .embody/config.json (user-local settings) and .embody/envoy.json
    (live runtime registry), project.json is intended to be checked into git
    so the same metadata travels with the repo to every machine.
    """
    return ext._findProjectRoot() / '.embody' / 'project.json'


def local_json_path(ext) -> Path:
    """Path to .embody/local.json -- MACHINE-LOCAL project metadata.

    Sibling of project.json but never committed (the .gitignore block
    ignores .embody/* and un-ignores only project.json). Holds keys that
    are true for THIS machine only -- today td_build, which churned in the
    tracked file whenever collaborators ran different TD builds (A-14).
    """
    return ext._findProjectRoot() / '.embody' / 'local.json'


def _write_json_atomic(path: Path, data: dict) -> None:
    """Atomic tmp+replace JSON write with the Windows retry (a reader
    holding the file open makes os.replace raise PermissionError). On
    final failure the orphan .tmp is removed before the raise."""
    import json, os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + '.tmp')
    content = json.dumps(data, indent=2) + '\n'
    try:
        for attempt in range(3):
            try:
                # pathlib.Path.write_text gained ``newline`` only in newer
                # Python releases. TouchDesigner/Convoy still support Python
                # 3.9 runtimes, so use the cross-version text-open API.
                with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(content)
                os.replace(str(tmp), str(path))
                return
            except PermissionError:
                if attempt < 2:
                    import time as _time
                    _time.sleep(0.1)
                else:
                    raise
    finally:
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_local_json(ext) -> None:
    """Pin the current TouchDesigner build into .embody/local.json.

    The Envoy bridge prefers this machine-local pin when picking a TD
    install to launch; the old committed pin churned per machine (A-14).
    Idempotent -- skips the write when td_build is already current. A
    corrupt local.json is self-healed (recreated, foreign keys lost) with
    a loud warning -- it is a regenerable machine-local cache, unlike the
    tracked project.json below, which is never overwritten blind. Note:
    every TD instance in a multi-instance repo writes here, so two
    instances on DIFFERENT TD builds will ping-pong the pin -- harmless
    for launch selection (either build launches), and readable keys are
    always merge-preserved.
    """
    import json
    path = local_json_path(ext)
    # app.build is the build proper (e.g. '2025.32460'). app.version is
    # the long-lived major branch ('099') and would only be noise here.
    current_build = app.build

    existing = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                existing = loaded
        except (ValueError, OSError) as e:
            # ValueError covers JSONDecodeError AND UnicodeDecodeError.
            ext.Log(
                f'.embody/local.json was unreadable ({e}) -- recreating '
                f'the machine-local pin file', 'WARNING')

    if existing.get('td_build') == current_build:
        return

    existing['td_build'] = current_build
    try:
        _write_json_atomic(path, existing)
        ext.Log(
            f'Pinned td_build={current_build} in .embody/local.json',
            'DEBUG')
    except Exception as e:
        ext.Log(f'Failed to write local.json: {e}', 'WARNING')


def write_project_json(ext) -> None:
    """Steward the COMMITTED .embody/project.json with key-level ownership.

    A-14 (Convoy plan): td_build was machine-specific and churned in this
    tracked file whenever collaborators ran different TD builds -- it now
    lives in .embody/local.json (write_local_json). This steward:

    - NEVER treats unreadable JSON as empty-and-overwrite: a corrupt or
      foreign-format file is left untouched with a loud warning, so a
      co-writer's keys (a future Convoy key, anything else) can never be
      destroyed by a parse failure.
    - Owns exactly the retired td_build key: removes it once (the one
      honest diff that ends the churn) and preserves every other key
      byte-for-byte.
    - Creates the file as {} when absent, keeping the committed
      placeholder the un-ignore rule and future keys expect.
    """
    import json
    path = project_json_path(ext)

    existing = {}
    if path.is_file():
        try:
            raw = path.read_text(encoding='utf-8')
        except (ValueError, OSError) as e:
            ext.Log(
                f'.embody/project.json is unreadable ({e}) -- leaving it '
                f'untouched (a tracked file with co-writers is never '
                f'overwritten blind). Fix or restore it from git.',
                'WARNING')
            return
        if not raw.strip():
            # A zero-byte / whitespace-only file holds nothing a co-writer
            # could lose -- the one corrupt shape the steward may heal.
            existing = {}
            action = 'Healed empty'
        else:
            try:
                loaded = json.loads(raw)
            except ValueError as e:  # JSONDecodeError included
                ext.Log(
                    f'.embody/project.json is unreadable ({e}) -- leaving '
                    f'it untouched (a tracked file with co-writers is '
                    f'never overwritten blind). Fix or restore it from '
                    f'git.', 'WARNING')
                return
            if not isinstance(loaded, dict):
                ext.Log(
                    '.embody/project.json is not a JSON object -- leaving '
                    'it untouched. Fix or restore it from git.', 'WARNING')
                return
            existing = loaded
            if 'td_build' not in existing:
                return  # nothing owned to change
            existing.pop('td_build')
            action = 'Retired td_build from'
    else:
        action = 'Created'

    try:
        if action == 'Created':
            # Exclusive create closes the check-then-write window: a
            # co-writer that lands the file first must not be clobbered
            # with {} -- A-14's guarantee. The next steward run handles
            # whatever they wrote.
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, 'x', encoding='utf-8', newline='\n') as f:
                    f.write(json.dumps(existing, indent=2) + '\n')
            except FileExistsError:
                ext.Log(
                    '.embody/project.json appeared mid-create (co-writer) '
                    '-- leaving it alone', 'DEBUG')
                return
        else:
            _write_json_atomic(path, existing)
        ext.Log(
            f'{action} .embody/project.json (machine-local pin lives in '
            f'.embody/local.json)', 'DEBUG')
    except Exception as e:
        ext.Log(f'Failed to write project.json: {e}', 'WARNING')


# --------------------------------------------------------------------------
# The 'convoy' key in .embody/project.json (Convoy Phase 2)
# --------------------------------------------------------------------------
# A-14 reserved this key when it retired td_build; Phase 2 is its first
# writer. Shape:
#
#     "convoy": {
#       "id": "cv_<16 lowercase hex>",
#       "binding_state": "candidate" | "established",
#       "consent_scope": "trusted LAN Convoy mesh",
#       "granted_at": "2026-08-01T09:12:33Z"
#     }
#
# Tracked on purpose (A-13 Model B): every clone of a repo converges on ONE
# convoy, which is safe because a convoy id is an IDENTIFIER, not a
# credential -- the group PSK is host-private and never leaves the host app.
CONVOY_KEY = 'convoy'
# Retained as the migration marker written by pre-LAN builds.
CONVOY_SCOPE_LOCAL = 'local host app only'
CONVOY_SCOPE_LAN = 'trusted LAN Convoy mesh'
CONVOY_BINDING_CANDIDATE = 'candidate'
CONVOY_BINDING_ESTABLISHED = 'established'
CONVOY_BINDING_STATES = frozenset((CONVOY_BINDING_CANDIDATE,
                                   CONVOY_BINDING_ESTABLISHED))


def mint_convoy_id() -> str:
    """A fresh convoy id: 'cv_' + 16 lowercase hex characters.

    The host app accepts any non-empty string, so this format is ours to
    keep stable. Prefixed so a convoy id can never be mistaken for a
    node_id or host_id (both bare 32-hex) in a log line or an audit record.
    """
    import secrets
    return 'cv_' + secrets.token_hex(8)


def _clean_convoy_id(value) -> str:
    """Canonical bounded project-facing Convoy identifier, or ``''``.

    This mirrors the host's public identity boundary without importing a
    separately installed host module into TouchDesigner.  Convoy IDs are
    identifiers, not credentials, but invisible/ambiguous text still must not
    enter tracked project metadata.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return ''
    try:
        raw = value.encode('utf-8')
    except UnicodeEncodeError:
        return ''
    if (len(raw) > 128
            or any(byte < 0x20 or byte == 0x7f for byte in raw)):
        return ''
    return value


def _entry_binding_state(entry: dict) -> str:
    """Safe rolling interpretation: an old persisted binding is established."""
    if not isinstance(entry, dict) or not _clean_convoy_id(entry.get('id')):
        return ''
    state = entry.get('binding_state')
    if state in CONVOY_BINDING_STATES:
        return state
    # Builds before automatic genesis persisted durable project IDs. Treating
    # those as fresh candidates could silently merge a moved/established
    # project into another LAN, so the only safe migration is established.
    return CONVOY_BINDING_ESTABLISHED


def _load_project_json(ext, purpose: str):
    """(data, readable) for the tracked .embody/project.json.

    Mirrors write_project_json's key-level-ownership discipline rather than
    refactoring it (that function is the shipped td_build retirement path
    and is deliberately left untouched): a tracked file with co-writers is
    NEVER overwritten blind, so unreadable or non-object JSON comes back as
    readable=False with a loud warning naming what was skipped, and every
    caller leaves the file exactly as it found it.

    A missing file reads as ({}, True) -- absence is not corruption; the
    writer below closes the check-then-write window with an exclusive
    create.
    """
    path = project_json_path(ext)
    if not path.is_file():
        return {}, True
    try:
        raw = path.read_text(encoding='utf-8')
    except (ValueError, OSError) as e:
        # ValueError covers UnicodeDecodeError.
        ext.Log(
            f'.embody/project.json is unreadable ({e}) -- leaving it '
            f'untouched, so {purpose} was skipped. Fix or restore it from '
            f'git.', 'WARNING')
        return None, False
    if not raw.strip():
        # A zero-byte / whitespace-only file holds nothing a co-writer
        # could lose -- the one corrupt shape a steward may heal.
        return {}, True
    try:
        loaded = json.loads(raw)
    except ValueError as e:  # JSONDecodeError included
        ext.Log(
            f'.embody/project.json is unreadable ({e}) -- leaving it '
            f'untouched, so {purpose} was skipped. Fix or restore it from '
            f'git.', 'WARNING')
        return None, False
    if not isinstance(loaded, dict):
        ext.Log(
            f'.embody/project.json is not a JSON object -- leaving it '
            f'untouched, so {purpose} was skipped. Fix or restore it from '
            f'git.', 'WARNING')
        return None, False
    return loaded, True


def read_convoy_entry(ext) -> dict:
    """The 'convoy' object from .embody/project.json, or {}.

    Read-only and TOTAL: an unreadable file, a missing key, or a value of
    the wrong shape all read as "no convoy recorded". It never raises,
    because one of its callers is a reconcile tick that must not be able to
    die on a hand-edited file.
    """
    data, readable = _load_project_json(ext, 'reading the convoy id')
    if not readable or not data:
        return {}
    entry = data.get(CONVOY_KEY)
    return entry if isinstance(entry, dict) else {}


def read_convoy_id(ext) -> str:
    """This project's convoy id, or '' when none is recorded."""
    return _clean_convoy_id(read_convoy_entry(ext).get('id'))


def read_convoy_binding_state(ext) -> str:
    """``candidate``/``established`` for a recorded binding, else ``''``."""
    return _entry_binding_state(read_convoy_entry(ext))


def ensure_convoy_id(ext, convoy_id=None,
                     consent_scope=CONVOY_SCOPE_LAN,
                     binding_state=CONVOY_BINDING_CANDIDATE) -> str:
    """Record the convoy key; return the id now in force, or '' on failure.

    KEY-LEVEL OWNERSHIP, exactly as write_project_json: this function owns
    the 'convoy' key and NOTHING else. Every other key is preserved
    byte-for-byte, an unreadable file is left untouched with a WARNING
    (never overwritten with {}), and a missing file is created with an
    EXCLUSIVE create so a co-writer that lands it first is not clobbered.

    IDEMPOTENT FOR ONE SCOPE: an already-recorded id wins and is never
    re-minted.  A different requested scope updates only consent metadata on
    that same id.  The caller is responsible for obtaining explicit local
    confirmation before requesting that migration; reconcile ticks only read
    this file and never call this writer.

    Called ONLY from the explicit-enable path (ConvoyExt._ensureConsent),
    never from a tick and never on project open: minting diffs a tracked
    file, so it happens when a human said yes.
    """
    path = project_json_path(ext)
    data, readable = _load_project_json(ext, 'recording the convoy id')
    if not readable:
        return ''

    from datetime import datetime, timezone
    scope = str(consent_scope or CONVOY_SCOPE_LAN).strip()
    if not scope or len(scope) > 128:
        ext.Log('Refused to record an invalid Convoy consent scope',
                'WARNING')
        return ''
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    requested_id = _clean_convoy_id(convoy_id) if convoy_id is not None else ''
    if convoy_id is not None and not requested_id:
        ext.Log('Refused to record an invalid Convoy id', 'WARNING')
        return ''
    if binding_state not in CONVOY_BINDING_STATES:
        ext.Log('Refused to record an invalid Convoy binding state', 'WARNING')
        return ''

    existing = data.get(CONVOY_KEY)
    if isinstance(existing, dict) and existing.get('id'):
        existing_id = _clean_convoy_id(existing.get('id'))
        if not existing_id:
            ext.Log('Refused to update a malformed existing Convoy id',
                    'WARNING')
            return ''
        existing_state = _entry_binding_state(existing)
        if (str(existing.get('consent_scope') or '') == scope
                and existing.get('binding_state') == existing_state):
            return existing_id
        entry = dict(existing)
        entry['id'] = existing_id
        # Consent maintenance must never demote a durable/legacy binding back
        # into genesis merely because ensure_convoy_id's new-entry default is
        # candidate.
        entry['binding_state'] = existing_state
        entry['consent_scope'] = scope
        entry['granted_at'] = now
    else:
        entry = {
            'id': requested_id or mint_convoy_id(),
            'binding_state': binding_state,
            'consent_scope': scope,
            'granted_at': now,
        }
    data[CONVOY_KEY] = entry
    try:
        if path.is_file():
            _write_json_atomic(path, data)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, 'x', encoding='utf-8', newline='\n') as f:
                    f.write(json.dumps(data, indent=2) + '\n')
            except FileExistsError:
                # A co-writer landed the file between the read and here.
                # Merge onto THEIR content instead of replacing it -- and if
                # they recorded a convoy first, theirs wins (one project,
                # one convoy).
                merged, ok = _load_project_json(
                    ext, 'recording the convoy id')
                if not ok:
                    return ''
                theirs = merged.get(CONVOY_KEY)
                if isinstance(theirs, dict) and theirs.get('id'):
                    # Their id wins, but this call follows a local consent
                    # confirmation, so apply the requested scope to that id
                    # instead of silently preserving an obsolete narrower
                    # grant or replacing the co-writer's identity.
                    theirs_id = _clean_convoy_id(theirs.get('id'))
                    if not theirs_id:
                        ext.Log('Refused to merge a malformed Convoy id',
                                'WARNING')
                        return ''
                    entry = dict(theirs)
                    entry['id'] = theirs_id
                    entry['binding_state'] = _entry_binding_state(theirs)
                    entry['consent_scope'] = scope
                    entry['granted_at'] = now
                merged[CONVOY_KEY] = entry
                _write_json_atomic(path, merged)
    except Exception as e:
        # Name the full target: a WinError 3 alone reports only the deepest
        # missing ancestor ('D:\'), which hid the real path in the field.
        ext.Log(f'Failed to record the convoy id in {path}: {e}',
                'WARNING')
        return ''
    ext.Log(
        f'Recorded convoy {entry["id"]} in .embody/project.json '
        f'(consent scope: {entry["consent_scope"]}). It is a TRACKED file, '
        f'so every clone of this repo shares the convoy.', 'INFO')
    return entry['id']


def adopt_convoy_id(ext, convoy_id, expected_id,
                    binding_state=CONVOY_BINDING_ESTABLISHED) -> str:
    """CAS-rebind this project's automatic realm; return the ID or ``''``.

    The authenticated local host app is the realm-convergence authority, but
    only the TD main thread owns tracked project metadata.  ``expected_id``
    prevents a stale registration response from overwriting a newer project
    binding after an extension reinit or a concurrent project writer.  Every
    non-Convoy key and all consent metadata are preserved.
    """
    new_id = _clean_convoy_id(convoy_id)
    expected = _clean_convoy_id(expected_id)
    if (not new_id or not expected
            or binding_state not in CONVOY_BINDING_STATES):
        ext.Log('Refused an invalid Convoy realm adoption', 'WARNING')
        return ''
    path = project_json_path(ext)
    data, readable = _load_project_json(ext, 'adopting the automatic Convoy')
    if not readable:
        return ''
    current = data.get(CONVOY_KEY)
    if not isinstance(current, dict) or _clean_convoy_id(
            current.get('id')) != expected:
        ext.Log('Skipped a stale Convoy realm adoption because the project '
                'binding changed', 'WARNING')
        return ''
    current_state = _entry_binding_state(current)
    if (current_state == CONVOY_BINDING_ESTABLISHED
            and new_id != expected):
        # An established project must enter the host's explicit conflict/reset
        # path, never silently follow a routine heartbeat to another realm.
        ext.Log('Refused to replace an established Convoy binding without '
                'an explicit local reset', 'WARNING')
        return ''
    if (current_state == CONVOY_BINDING_ESTABLISHED
            and binding_state == CONVOY_BINDING_CANDIDATE):
        ext.Log('Refused to demote an established Convoy binding', 'WARNING')
        return ''

    if new_id == expected and current.get('binding_state') == binding_state:
        return new_id
    from datetime import datetime, timezone
    entry = dict(current)
    entry['id'] = new_id
    entry['binding_state'] = binding_state
    entry['bound_at'] = datetime.now(timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')
    data[CONVOY_KEY] = entry
    try:
        _write_json_atomic(path, data)
    except Exception as e:
        ext.Log(f'Failed to adopt the automatic Convoy realm: {e}',
                'WARNING')
        return ''
    ext.Log(f'Adopted automatic Convoy realm {new_id} '
            f'({binding_state}) in .embody/project.json', 'INFO')
    return new_id


def rebind_convoy_to_candidate(ext, expected_id) -> str:
    """USER-CONFIRMED demotion of this project's realm binding to candidate.

    The one sanctioned way out of an established binding the local
    daemon's realm refuses. adopt_convoy_id deliberately hard-refuses
    both replacing and demoting an established binding on the automatic
    paths ('never silently follow a routine heartbeat to another
    realm') -- which left a project cloned onto a different LAN refused
    forever, with the refusal message naming an 'explicit local reset'
    that did not exist. This is that reset: gated behind the user's
    Rejoin confirmation on the explicit re-enable gesture, never called
    from a tick. The id is KEPT -- a candidate id is authority-free and
    the host's convergence rules replace it with the local realm on the
    next register -- consent metadata is preserved, and bound_at is
    dropped because the binding it stamped no longer holds.
    """
    expected = _clean_convoy_id(expected_id)
    if not expected:
        ext.Log('Refused an invalid Convoy rejoin (no expected id)',
                'WARNING')
        return ''
    path = project_json_path(ext)
    data, readable = _load_project_json(ext, 'rejoining the local Convoy')
    if not readable:
        return ''
    current = data.get(CONVOY_KEY)
    if not isinstance(current, dict) or _clean_convoy_id(
            current.get('id')) != expected:
        ext.Log('Skipped a stale Convoy rejoin because the project '
                'binding changed', 'WARNING')
        return ''
    entry = dict(current)
    entry['binding_state'] = CONVOY_BINDING_CANDIDATE
    entry.pop('bound_at', None)
    data[CONVOY_KEY] = entry
    try:
        _write_json_atomic(path, data)
    except Exception as e:
        ext.Log(f'Failed to rebind the Convoy binding to candidate: {e}',
                'WARNING')
        return ''
    ext.Log(f'Rebound Convoy {expected} to candidate in '
            '.embody/project.json (user-confirmed rejoin; the local '
            'realm is adopted on the next register)', 'INFO')
    return expected


def save_settings(ext) -> None:
    """Persist whitelisted parameter values to .embody/config.json."""
    ext._settings_save_pending = False
    params = {}
    # Sort names so JSON output is stable across TD sessions. _PERSISTED_PARAMS
    # is a frozenset, and Python's hash randomization gives each process a
    # different iteration order -- producing noisy diffs on every save.
    for name in sorted(ext._PERSISTED_PARAMS):
        par = getattr(ext.my.par, name, None)
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
    path = None
    try:
        import json, os
        path = settings_path(ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + '.tmp')
        content = json.dumps(data, indent=2, sort_keys=True) + '\n'
        for attempt in range(3):
            try:
                tmp.write_text(content, encoding='utf-8', newline='\n')
                os.replace(str(tmp), str(path))
                # DEBUG breadcrumb: persistence in UNTITLED projects
                # depends on where this resolves (issue #60) -- when an
                # "Always" answer doesn't survive a relaunch, this line
                # is the diagnostic.
                ext.Log(f'Settings saved to {path}', 'DEBUG')
                return
            except PermissionError:
                if attempt < 2:
                    import time as _time
                    _time.sleep(0.1)
                else:
                    raise
    except Exception as e:
        ext.Log(f'Failed to save settings to {path or "config.json"}: {e}',
                'WARNING')


def defer_save_settings(ext) -> None:
    """Schedule a settings save on the next frame. Coalesces rapid changes."""
    if not getattr(ext, '_settings_save_pending', False):
        ext._settings_save_pending = True
        run(f"op('{ext.my}').ext.Embody._saveSettings()", delayFrames=1)


def normalize_legacy_par_keys(params: dict, renames: dict) -> dict:
    """Map TDN-era config.json keys onto their current TDXN names.

    config.json is keyed by PARAMETER NAME, so renaming a parameter
    without this drops that setting back to its default on every existing
    install -- silently, because a missing key is indistinguishable from
    "never set". Pure and side-effect free so it can be tested against a
    real legacy config; `renames` is EmbodyExt._TDXN_PAR_RENAMES, keyed by
    ParGroup BASE name, so RGBA components (Tdntagcolorr) are matched by
    stripping their trailing component letter.

    Keys with no mapping pass through untouched -- notably 'Tdnenable',
    the pre-6.1 key the mode-migration nudge still reads.
    """
    if not renames:
        return params
    out = {}
    for key, entry in params.items():
        base, suffix = key, ''
        if (key not in renames
                and key[-1:] in ('r', 'g', 'b', 'a')
                and key[:-1] in renames):
            base, suffix = key[:-1], key[-1]
        out[renames.get(base, base) + suffix] = entry
    return out


# A stored value that is merely the OLD SHIPPED DEFAULT was never a user
# choice, so carrying it forward pins every existing install on the retired
# spelling forever. A value the user actually customized is left alone.
_LEGACY_PAR_DEFAULTS = {
    'Tdxntag': ('tdn', 'tdxn'),
    'Tdxnexcludetag': ('tdn_exclude', 'tdxn_exclude'),
}


def normalize_legacy_par_values(params: dict) -> dict:
    """Upgrade stored values that are only the retired shipped default.

    Pure and side-effect free (testable against a real legacy config).
    Only a constant-mode entry is touched -- an expression or bind is the
    user's own, whatever it evaluates to.
    """
    out = {}
    for key, entry in params.items():
        pair = _LEGACY_PAR_DEFAULTS.get(key)
        if (pair and isinstance(entry, dict) and not entry.get('mode')
                and entry.get('val') == pair[0]):
            entry = dict(entry, val=pair[1])
        out[key] = entry
    return out


def restore_settings(ext, kick_envoy: bool = False) -> bool:
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
    path = find_settings_file(ext)
    if path is None:
        # Migrate: check old root-level .embody.json
        canonical = settings_path(ext)
        old_path = ext._findProjectRoot() / '.embody.json'
        if old_path.is_file():
            try:
                canonical.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.move(str(old_path), str(canonical))
                ext.Log('Migrated .embody.json -> .embody/config.json', 'INFO')
                path = canonical
            except Exception as e:
                ext.Log(f'Could not migrate .embody.json: {e}', 'WARNING')
                ext.my.store('_init_complete', True)
                return False
        else:
            ext.my.store('_init_complete', True)
            return False
    try:
        import json
        data = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        ext.Log(f'Settings file corrupt or unreadable: {e}', 'WARNING')
        ext.my.store('_init_complete', True)
        return False
    if not isinstance(data, dict) or 'params' not in data:
        ext.my.store('_init_complete', True)
        return False
    params = data['params']
    # config.json is keyed by PARAMETER NAME, so the TDXN -> TDXN rename
    # would silently drop each of these settings back to its default on
    # every existing install (see normalize_legacy_par_keys).
    params = normalize_legacy_par_keys(
        params, getattr(ext, '_TDXN_PAR_RENAMES', None) or {})
    params = normalize_legacy_par_values(params)
    restored = 0
    ext._restoring_settings = True
    try:
        for name, entry in params.items():
            par = getattr(ext.my.par, name, None)
            if par is None or name not in ext._PERSISTED_PARAMS:
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
        ext._restoring_settings = False
    # Signal parexec that init + restore is complete -- safe to process
    # param changes.  Must be stored AFTER _restoring_settings is cleared
    # so deferred onValueChange callbacks from init() are still suppressed.
    ext.my.store('_init_complete', True)
    ext.Log(f'Restored {restored} settings from config.json', 'INFO')
    # TDXN mode migration detection: an upgrading user will have
    # 'Tdnenable' in their persisted params but not 'Tdxnmode'. Defer
    # the nudge dialog so init can complete cleanly first.
    # Guarded by a schedule-time flag so a second _restoreSettings in
    # the same session (e.g. onCreate then onStart) can't queue a
    # second dialog before the first one fires.
    already_scheduled = ext.my.fetch(
        '_tdn_migration_scheduled', False, search=False)
    if ('Tdnenable' in params and 'Tdxnmode' not in params
            and not already_scheduled):
        prev_tdxn_enable = bool(params.get('Tdnenable', {}).get('val', True))
        ext.my.store('_tdn_migration_prev_enable', prev_tdxn_enable)
        ext.my.store('_tdn_migration_scheduled', True)
        run(f"op('{ext.my}').ext.Embody._showTDXNMigrationNudge()",
            delayFrames=60)
    # If Envoyenable was restored to True, kick Start() -- parexec was
    # suppressed during restore so onValueChange never fired.
    # Only set this on the onStart() path (kick_envoy=True).
    # Verify() owns Envoy startup on the onCreate() path.
    if kick_envoy and ext.my.par.Envoyenable.eval():
        run(f"op('{ext.my}').ext.Envoy.Start()", delayFrames=3)
    return restored > 0


def show_tdxn_migration_nudge(ext) -> None:
    """One-time dialog after upgrading from the binary Tdnenable toggle.

    Fires when a user opens a project previously saved with the old
    Tdnenable toggle and no Tdxnmode selection yet. Offers a choice
    between restoring Full bidirectional sync (their prior behavior)
    or adopting the new Export-on-Save default (recommended).

    Guarded by _tdn_mode_migration_shown so it only fires once per
    project across sessions (the flag is persisted via param write
    into config.json on next save).
    """
    if ext.my.fetch('_tdn_mode_migration_shown', False, search=False):
        return
    prev_enable = ext.my.fetch('_tdn_migration_prev_enable', True,
                               search=False)
    ext.my.unstore('_tdn_migration_prev_enable')

    tdxn_comps = []
    try:
        tdxn_comps = ext._getTDXNStrategyComps()
    except Exception:
        pass

    if not tdxn_comps:
        # No TDXN COMPs tracked -- silently accept the new default.
        ext.my.store('_tdn_mode_migration_shown', True)
        return

    prev_label = ('Full (bidirectional)' if prev_enable
                  else 'Off (TDXN disabled)')
    msg = (
        f'TDXN default changed in this release.\n\n'
        f'Your project was previously saved with the legacy Tdnenable '
        f'toggle ({prev_label}). The new system has three modes:\n\n'
        f'  \u2022 Off -- no TDXN runtime\n'
        f'  \u2022 Export-on-Save -- recommended; .toe is truth, '
        f'.tdn files are rewritten on save\n'
        f'  \u2022 Roundtrip (Experimental) -- bidirectional '
        f'strip/restore on save and reconstruction on open (previous '
        f'behavior)\n\n'
        f'Currently set to Export-on-Save. Your {len(tdxn_comps)} '
        f'tracked TDXN COMP(s) will stop round-tripping on save.\n\n'
        f'Keep the new default, or restore Full?'
    )
    choice = ext._messageBox(
        'Embody - TDXN Mode Changed',
        msg,
        buttons=['Keep Export-on-Save (recommended)',
                 'Restore Full (previous behavior)'])
    if choice == 1:
        try:
            ext.my.par.Tdxnmode = 'full'
            ext._applyTdxnModeGating()
            ext.Log('TDXN mode restored to Full per user choice', 'INFO')
        except Exception as e:
            ext.Log(f'Could not restore Full mode: {e}', 'WARNING')
    else:
        ext.Log('TDXN mode kept at Export-on-Save (new default)', 'INFO')
    ext.my.store('_tdn_mode_migration_shown', True)
