"""EmbodyExt uninstall + settings persistence (module DAT).

Module DAT (mod.embody_admin) called by EmbodyExt on the MAIN THREAD only (the
ext-diet WP7c + WP7d clusters C5 + C9). Holds:

  - C5  Uninstall: the NON-DESTRUCTIVE planner (compute_uninstall_plan) +
        preview_uninstall, and the DESTRUCTIVE executor (execute_uninstall_plan)
        with its file-safe helpers (remove_tree_within / strip_marked_block /
        strip_mcp_envoy) + the promoted Uninstall entry point. The uninstall
        marker constants (_UNINSTALL_MARKER_*) live at module scope -- their sole
        consumer is compute_uninstall_plan and nothing external reads them.
  - C9  Settings/config.json persistence: settings_path / find_settings_file /
        project_json_path / write_project_json / save_settings /
        defer_save_settings / restore_settings / show_tdn_migration_nudge.

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
_venvPaths / _uninstallClassifyMarker / _messageBox / _getTDNStrategyComps /
_applyTdnModeGating. The class attr _PERSISTED_PARAMS stays on EmbodyExt (read by
parexec.py) and is reached via ext._PERSISTED_PARAMS. Instance state
(_settings_save_pending, _restoring_settings) lives on the ext, unchanged.

The run() deferral strings target the facade stubs (ext.Embody._saveSettings /
._showTDNMigrationNudge / ext.Envoy.Start) so they resolve after the move.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional


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

_UNINSTALL_MARKER_FILES = ('AGENTS.md', 'CLAUDE.md', 'ENVOY.md', 'GEMINI.md')
_UNINSTALL_MARKER_TREES = ('.claude/rules', '.claude/skills', '.cursor/rules',
                           '.github/instructions', '.windsurf/rules')
_UNINSTALL_MARKER_SINGLES = ('.github/copilot-instructions.md',)


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
        elif cls == 'review':
            _add('review', p, why='you edited this generated file -- kept')

    # ---- manifest (precise) ----
    if any((m.get('files_created'), m.get('files_appended'),
            m.get('git_config'), m.get('venv'))):
        plan['sources'].append('manifest')

    for stored in m.get('files_created', []):
        p = _abs(stored)
        if p.name == '.mcp.json':
            if p.exists():
                _add_strip(p, 'json_key', 'mcpServers.envoy',
                           'remove Embody server; delete file only if none remain')
            continue
        if p.name == 'opencode.json':
            if p.exists():
                _add_strip(p, 'json_key', 'mcp.envoy',
                           'remove Embody server + instructions entry; '
                           'delete file only if nothing else remains')
            continue
        if not p.exists():
            plan['missing'].append(stored); continue
        if p.name == 'settings.local.json':
            _add('review', p,
                 why='created by Embody but may hold your permission edits -- kept')
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
            _add('delete', p, kind='dir', why='Embody-created virtual environment')
        else:
            plan['missing'].append(v['path'])

    # ---- marker-scan FALLBACK (pre-manifest installs / anything missed) ----
    before = sum(len(plan[b]) for b in ('delete', 'review', 'strip')) + len(plan['unset'])
    for name in _UNINSTALL_MARKER_FILES:
        p = root / name
        if p.is_file():
            _classify_into(p)
    for sub in _UNINSTALL_MARKER_TREES:
        d = root / sub
        if d.is_dir():
            for p in d.rglob('*'):
                if p.is_file():
                    _classify_into(p)
    for single in _UNINSTALL_MARKER_SINGLES:
        p = root / single
        if p.is_file():
            _classify_into(p)
    mcp = root / '.mcp.json'
    if mcp.is_file() and str(mcp.resolve()) not in seen:
        try:
            if 'envoy' in json.loads(
                    mcp.read_text(encoding='utf-8')).get('mcpServers', {}):
                _add_strip(mcp, 'json_key', 'mcpServers.envoy',
                           'remove Embody server; delete file only if none remain')
        except Exception:
            pass
    oc = root / 'opencode.json'
    if oc.is_file() and str(oc.resolve()) not in seen:
        try:
            if 'envoy' in json.loads(
                    oc.read_text(encoding='utf-8')).get('mcp', {}):
                _add_strip(oc, 'json_key', 'mcp.envoy',
                           'remove Embody server + instructions entry; '
                           'delete file only if nothing else remains')
        except Exception:
            pass
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
        for key in ('diff.tdn.textconv', 'diff.tdn.cachetextconv'):
            try:
                r = subprocess.run(['git', 'config', '--get', key],
                                   cwd=str(root), capture_output=True,
                                   text=True, timeout=5,
                                   stdin=subprocess.DEVNULL)
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


def strip_mcp_envoy(ext, path):
    """Remove only Embody's entries from a .mcp.json OR opencode.json.

    Shape-aware: .mcp.json keeps servers under 'mcpServers'; opencode.json
    keeps them under 'mcp' and additionally carries Embody's generated
    '.claude/rules/*.md' instructions entry (see envoy_setup.
    write_opencode_config). Never writes one shape's keys into the other's
    file. Delete the file only if stripping leaves nothing but boilerplate
    ('$schema') -- the user's servers/keys are always preserved."""
    try:
        cfg = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return
    is_opencode = path.name == 'opencode.json' or (
        'mcp' in cfg and 'mcpServers' not in cfg)
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
        path.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')
        return
    cfg.pop(key, None)
    leftover = [k for k in cfg if k not in ('$schema',)]
    if is_opencode and leftover == ['permission']:
        # Only Embody's fresh-file permission block remains -> ours.
        leftover = []
    if leftover:
        path.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')
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
            if entry.get('kind') == 'dir':
                if p.is_dir():
                    remove_tree_within(ext, p, root)
                    summary['deleted'] += 1
            elif p.exists():
                p.unlink(); summary['deleted'] += 1
        except OSError as e:
            summary['errors'] += 1
            ext.Log(f'Uninstall: could not remove {p}: {e}', 'WARNING')

    for entry in plan['delete']:
        _remove(entry)

    for a in plan['strip']:
        p = _abs(a['path'])
        if not p.exists():
            continue
        try:
            if a['kind'] == 'json_key':
                strip_mcp_envoy(ext, p)
            else:
                p.write_text(
                    strip_marked_block(
                        ext, p.read_text(encoding='utf-8'), a['marker']),
                    encoding='utf-8')
            summary['stripped'] += 1
        except OSError as e:
            summary['errors'] += 1
            ext.Log(f'Uninstall: could not strip {p}: {e}', 'WARNING')

    for key in plan['unset']:
        try:
            subprocess.run(['git', 'config', '--unset', key],
                           cwd=str(root), capture_output=True, text=True,
                           timeout=5, stdin=subprocess.DEVNULL)
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


def write_project_json(ext) -> None:
    """Pin the current TouchDesigner build into .embody/project.json.

    The Envoy bridge reads td_build to pick a matching TD install when
    launching on a fresh clone, where envoy.json is gitignored and its
    td_executable path may not exist locally. Idempotent -- skips the
    write when td_build is already current.
    """
    import json, os
    path = project_json_path(ext)
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
                ext.Log(
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
        ext.Log(f'Failed to write project.json: {e}', 'WARNING')


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
    try:
        import json, os
        path = settings_path(ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + '.tmp')
        content = json.dumps(data, indent=2, sort_keys=True) + '\n'
        for attempt in range(3):
            try:
                tmp.write_text(content, encoding='utf-8')
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
        ext.Log(f'Failed to save settings: {e}', 'WARNING')


def defer_save_settings(ext) -> None:
    """Schedule a settings save on the next frame. Coalesces rapid changes."""
    if not getattr(ext, '_settings_save_pending', False):
        ext._settings_save_pending = True
        run(f"op('{ext.my}').ext.Embody._saveSettings()", delayFrames=1)


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
    # TDN mode migration detection: an upgrading user will have
    # 'Tdnenable' in their persisted params but not 'Tdnmode'. Defer
    # the nudge dialog so init can complete cleanly first.
    # Guarded by a schedule-time flag so a second _restoreSettings in
    # the same session (e.g. onCreate then onStart) can't queue a
    # second dialog before the first one fires.
    already_scheduled = ext.my.fetch(
        '_tdn_migration_scheduled', False, search=False)
    if ('Tdnenable' in params and 'Tdnmode' not in params
            and not already_scheduled):
        prev_tdn_enable = bool(params.get('Tdnenable', {}).get('val', True))
        ext.my.store('_tdn_migration_prev_enable', prev_tdn_enable)
        ext.my.store('_tdn_migration_scheduled', True)
        run(f"op('{ext.my}').ext.Embody._showTDNMigrationNudge()",
            delayFrames=60)
    # If Envoyenable was restored to True, kick Start() -- parexec was
    # suppressed during restore so onValueChange never fired.
    # Only set this on the onStart() path (kick_envoy=True).
    # Verify() owns Envoy startup on the onCreate() path.
    if kick_envoy and ext.my.par.Envoyenable.eval():
        run(f"op('{ext.my}').ext.Envoy.Start()", delayFrames=3)
    return restored > 0


def show_tdn_migration_nudge(ext) -> None:
    """One-time dialog after upgrading from the binary Tdnenable toggle.

    Fires when a user opens a project previously saved with the old
    Tdnenable toggle and no Tdnmode selection yet. Offers a choice
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

    tdn_comps = []
    try:
        tdn_comps = ext._getTDNStrategyComps()
    except Exception:
        pass

    if not tdn_comps:
        # No TDN COMPs tracked -- silently accept the new default.
        ext.my.store('_tdn_mode_migration_shown', True)
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
    choice = ext._messageBox(
        'Embody - TDN Mode Changed',
        msg,
        buttons=['Keep Export-on-Save (recommended)',
                 'Restore Full (previous behavior)'])
    if choice == 1:
        try:
            ext.my.par.Tdnmode = 'full'
            ext._applyTdnModeGating()
            ext.Log('TDN mode restored to Full per user choice', 'INFO')
        except Exception as e:
            ext.Log(f'Could not restore Full mode: {e}', 'WARNING')
    else:
        ext.Log('TDN mode kept at Export-on-Save (new default)', 'INFO')
    ext.my.store('_tdn_mode_migration_shown', True)
