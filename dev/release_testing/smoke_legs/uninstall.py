"""
Uninstall leg -- the last thing the release smoke does to its project.

Runs after the upgrade and faults legs, against the project where Embody
was installed for this smoke. It proves the reversal contract the product
publishes (docs/embody/parameters.md, the `Uninstall` row):

    deletes Embody-generated config, Embody's .venv and .embody/ state;
    strips only Embody's key/block from shared files (.mcp.json, ...);
    your own files, the externalized .tox/.tdxn/.py and the Embody COMP
    are NOT removed.

Method: confirm the port answers for THIS run dir, plant four pieces of
USER content (a plain file, a hand-written rule inside Embody's own
.claude/rules/, a second MCP server in .mcp.json, a key in
settings.local.json), hash them, take the plan from PreviewUninstall, run
the Uninstall the BUTTON runs, then read the answer off the filesystem --
Envoy is stopped by Uninstall itself (embody_admin.uninstall), so nothing
after it can be an MCP call.

Two invariants decide this leg:
  - the plan is derived from .embody/manifest.json, never from a hand-list:
    every recorded path must be accounted for in some bucket, and a file
    the user wrote must appear in none of them;
  - every path outside run_dir is untouched -- the plan's own root is
    refused unless it IS the run dir, and the repo checkout's Embody files
    are hashed before and after (the 2026-07-27 incident: a smoke rooted
    in the repo stripped 16 committed files).

Nothing here deletes anything, and nothing here quits TouchDesigner: the
orchestrator's teardown runs after this leg and finds Envoy gone, so
quit_smoke_td falls through its MCP rung to the pid-scoped close/force
ladder. The run directory is left for the operator.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

# Planted user content. The rule file sits INSIDE .claude/rules/, which
# Uninstall marker-sweeps file by file and then rmdirs -- it is the one
# place where "only Embody's files" and "the directory goes" collide.
USER_FILE = 'user_notes.txt'
USER_RULE = '.claude/rules/mine.md'
USER_SERVER = 'smoke_user_server'
USER_SETTING = 'smokeUserSetting'
SETTINGS_REL = '.claude/settings.local.json'
SUMMARY_FILE = 'uninstall_summary.json'
# Embody's marker must not appear in planted content: a file carrying it at
# the top is classified "ours" (embody_git.is_generated_by_embody).
USER_NOTES_TEXT = ('my own notes -- not Embody\'s, and Uninstall must not\n'
                   'touch this file.\n')
USER_RULE_TEXT = ('# my rule\n\nHand written. No marker, no Embody block.\n')

# Every ui.messageBox uninstall_handler can raise (embody_admin.py):
# 'Embody -- Uninstall' is both the nothing-to-do notice [OK] and the
# confirm [Cancel, Uninstall] -- 1 answers the confirm. An unseeded title
# returns -1 (EmbodyExt._messageBox) while the bootstrap's sentinel keeps
# the store alive, so a miss cancels the run rather than freezing TD.
SMOKE_SENTINEL = '__smoke_sentinel__'
DIALOG_RESPONSES = {'Embody -- Uninstall': 1,
                    'Embody -- Uninstall Complete': 0,
                    SMOKE_SENTINEL: 0}

SUMMARY_TIMEOUT_S = 180
ENVOY_DOWN_TIMEOUT_S = 45
# Left on the run's ceiling for the orchestrator's teardown: every wait
# here stops this much short of budget() so the quit ladder still fits.
TEARDOWN_RESERVE_S = 60
# Files in the repo checkout that Embody owns there. Hashed before and
# after as the outside-the-run-dir fence; the first four are committed, so
# a fresh CI clone always has real hashes to compare.
REPO_WITNESS = ('CLAUDE.md', 'AGENTS.md', '.gitignore', '.gitattributes',
                '.mcp.json', SETTINGS_REL, '.embody/manifest.json')
# Not walked for survivors: TD rotates logs and writes backups on its own
# schedule, so a file vanishing there is not Uninstall's doing. The
# directories themselves are still in the top-level listing.
VOLATILE_DIRS = ('logs', 'Backup')
# Embody re-creates these right after its own Uninstall: setting
# Envoyenable=0 fires parexec onValueChange -> _deferSaveSettings, and that
# run(delayFrames=1) lands AFTER the deletion, mkdir-ing .embody/ to write
# config.json (embody_admin.save_settings). Reported, never counted as a
# removal that failed -- and never an excuse for the install record.
REBORN = ('.embody', '.embody/config.json')


def _sha256(path):
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(data, indent=2) + '\n')


def _norm(path):
    """Comparable spelling: POSIX separators, no trailing slash, case-folded
    where the filesystem is. The manifest stores POSIX, the plan stores
    whatever Path.resolve() produced -- they have to meet somewhere."""
    p = str(path or '').replace('\\', '/').rstrip('/')
    return p.lower() if os.name == 'nt' else p


def _same_dir(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _norm(os.path.abspath(a)) == _norm(os.path.abspath(b))


def _plan_paths(plan, bucket):
    return {_norm(e.get('path', '')) for e in plan.get(bucket, [])}


def confirm_target(ctx):
    """(ok, detail): the port answers for THIS run dir. Nothing is planted
    and nothing is uninstalled until it does -- a collided or stale port
    would otherwise carry the uninstall into another project (the same gate
    smoke_run.probe_mcp takes before it mutates anything)."""
    try:
        folder = str(ctx['py']('result = project.folder'))
    except Exception as e:
        return False, 'project.folder unreadable: %s: %s' % (
            type(e).__name__, e)
    return (_same_dir(folder, ctx['run_dir']),
            'port %s serves project.folder=%s' % (ctx.get('port'), folder))


def plant_user_content(run_dir):
    """Create the four pieces of user content and hash them.

    Returns {'hashes': {rel: sha}, 'notes': [str], 'problems': [str],
    'planted': {rel: bool}}: a piece that cannot be written is reported,
    never raised, and the leg skips the assertion that depended on it.
    """
    out = {'hashes': {}, 'notes': [], 'problems': [], 'planted': {}}

    for rel, text in ((USER_FILE, USER_NOTES_TEXT),
                      (USER_RULE, USER_RULE_TEXT)):
        path = os.path.join(run_dir, rel)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
            out['hashes'][rel] = _sha256(path)
            out['planted'][rel] = True
        except OSError as e:
            out['problems'].append('%s: %s' % (rel, e))
            out['planted'][rel] = False

    mcp_path = os.path.join(run_dir, '.mcp.json')
    cfg = _read_json(mcp_path)
    if cfg is None:
        out['problems'].append(
            'no readable .mcp.json at %s -- the install should have written '
            'one' % mcp_path)
        out['planted']['.mcp.json'] = False
    else:
        servers = cfg.setdefault('mcpServers', {})
        servers[USER_SERVER] = {'type': 'stdio', 'command': 'echo',
                                'args': ['mine']}
        _write_json(mcp_path, cfg)
        out['hashes']['.mcp.json'] = _sha256(mcp_path)
        out['planted']['.mcp.json'] = True
        out['notes'].append('.mcp.json: added %s beside %s' % (
            USER_SERVER, sorted(k for k in servers if k != USER_SERVER)))

    set_path = os.path.join(run_dir, SETTINGS_REL)
    settings = _read_json(set_path)
    if settings is None:
        out['problems'].append('no readable %s -- review bucket unchecked'
                               % SETTINGS_REL)
        out['planted'][SETTINGS_REL] = False
    else:
        settings[USER_SETTING] = 'keep-me'
        _write_json(set_path, settings)
        out['hashes'][SETTINGS_REL] = _sha256(set_path)
        out['planted'][SETTINGS_REL] = True
    return out


def manifest_expectations(run_dir, manifest):
    """(recorded, venv_rel): every path Embody's install record names, and
    the recorded venv. The plan must ACCOUNT for each recorded path in some
    bucket -- which bucket is the product's classification to make, and
    re-deriving it here would only be a second hand-list to drift."""
    recorded = {_norm(p) for p in manifest.get('files_created', [])}
    recorded |= {_norm(e.get('path', ''))
                 for e in manifest.get('files_appended', [])}
    recorded.discard('')
    venv = (manifest.get('venv') or {}).get('path')
    if venv and not os.path.isdir(os.path.join(run_dir, venv)):
        venv = None
    return recorded, (_norm(venv) if venv else None)


def survivors(run_dir, plan):
    """Every path the plan does NOT touch -- each one must still exist
    afterwards. Top-level entries plus a full walk, PRUNED at any planned
    directory (.venv, .embody): those go wholesale, so walking them would
    only re-state the plan. The walk is what catches a nested file --
    an externalized .tdxn two levels down leaves its directory standing,
    so a top-level listing alone reads clean while the file is gone."""
    planned = (_plan_paths(plan, 'delete') | _plan_paths(plan, 'strip')
               | _plan_paths(plan, 'review'))
    out = [name for name in sorted(os.listdir(run_dir))
           if _norm(name) not in planned]
    for base, dirs, files in os.walk(run_dir):
        rel_base = os.path.relpath(base, run_dir).replace('\\', '/')
        rel_base = '' if rel_base == '.' else rel_base + '/'
        dirs[:] = [d for d in dirs
                   if _norm(rel_base + d) not in planned
                   and rel_base + d not in VOLATILE_DIRS]
        for name in files:
            rel = rel_base + name
            if _norm(rel) not in planned and rel not in out:
                out.append(rel)
    return out


def state_files(run_dir):
    """Every file under .embody/, POSIX-relative to run_dir."""
    out = []
    for base, _dirs, files in os.walk(os.path.join(run_dir, '.embody')):
        for name in files:
            rel = os.path.relpath(os.path.join(base, name), run_dir)
            out.append(rel.replace('\\', '/'))
    return sorted(out)


def repo_witness_hashes(repo):
    """sha256 of the repo checkout's own Embody-generated files (None where
    absent -- .mcp.json and .embody/ are gitignored and miss on a fresh
    clone). The fence: the plan is refused unless its root IS the run dir,
    and embody_admin.remove_tree_within refuses anything outside it."""
    if not repo:
        return {}
    return {rel: _sha256(os.path.join(repo, rel)) for rel in REPO_WITNESS}


def uninstall_script(summary_file=SUMMARY_FILE, responses=None):
    """The deferred script the leg hands to TD's main thread.

    Drives the product's own button path (parexec onPulse -> uninstall_
    handler), with the confirm dialog seeded and MERGED into the store so
    the bootstrap's sentinel survives. Uninstall turns Envoyenable off
    before it deletes anything, so the reply to a direct call would die
    with the server: deferring lets the MCP reply return first and the
    summary comes back through the filesystem instead -- written via
    os.replace, so a poller never reads half. Never quits TD.
    """
    return (
        "import json, os\n"
        "_dest = os.path.join(project.folder, %r)\n"
        "_emb = op.Embody\n"
        "_seed = _emb.fetch('_smoke_test_responses', None, search=False)\n"
        "if not isinstance(_seed, dict):\n"
        "    _seed = {}\n"
        "_seed.update(%r)\n"
        "_emb.store('_smoke_test_responses', _seed)\n"
        "try:\n"
        "    _s = _emb.ext.Embody.uninstallHandler()\n"
        "except Exception as _e:\n"
        "    _s = {'error': type(_e).__name__ + ': ' + str(_e)}\n"
        "if not isinstance(_s, dict):\n"
        "    _s = {'error': 'uninstallHandler returned ' + repr(_s)}\n"
        "with open(_dest + '.tmp', 'w') as _f:\n"
        "    _f.write(json.dumps(_s, default=str))\n"
        "os.replace(_dest + '.tmp', _dest)\n"
        % (summary_file, dict(responses or DIALOG_RESPONSES)))


def defer(ctx, script, delay_ms=1500):
    """Hand `script` to the smoke TD's main thread and return at once --
    mandatory here, since the reply to this very call travels over the
    socket Uninstall is about to close. `run` is not in execute_python's
    namespace (EnvoyExt._execNamespace), hence the import."""
    return ctx['py']("from td import run as _run\n"
                     "_run(%r, fromOP=op.Embody, delayMilliSeconds=%d)\n"
                     "result = 'scheduled'" % (script, int(delay_ms)))


def button_wiring(ctx):
    """What the Uninstall BUTTON is: the pulse par's style and the DATs
    routing it to uninstallHandler. Read live, because that routing is the
    path this leg drives and nothing else in the smoke touches it."""
    code = (
        "import json\n"
        "_out = {'style': '', 'routers': []}\n"
        "try:\n"
        "    _out['style'] = op.Embody.par.Uninstall.style\n"
        "except Exception as _e:\n"
        "    _out['error'] = str(_e)\n"
        "for _d in op.Embody.children:\n"
        "    if not getattr(_d, 'isDAT', False):\n"
        "        continue\n"
        "    _t = _d.text or ''\n"
        "    if \"'Uninstall'\" in _t and 'uninstallHandler' in _t:\n"
        "        _out['routers'].append(_d.name)\n"
        "result = json.dumps(_out)\n")
    return json.loads(str(ctx['py'](code)))


def envoy_is_down(ctx, timeout=ENVOY_DOWN_TIMEOUT_S, clock=None, sleep=None):
    """True once the smoke Envoy stops answering. Uninstall sets
    Envoyenable=0 (embody_admin.uninstall), so a port still serving tools
    after it means the server outlived its own uninstall."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + timeout
    while True:
        try:
            ctx['py']('result = 1', timeout=5)
        except Exception:
            return True
        if clock() >= deadline or ctx['budget']() <= TEARDOWN_RESERVE_S:
            return False
        sleep(2)


def run(ctx):
    steps = []

    def step(name, ok, detail=''):
        steps.append({'step': name, 'ok': bool(ok), 'detail': str(detail)})
        return bool(ok)

    def done(error=''):
        return {'ok': bool(steps) and all(s['ok'] for s in steps) and
                not error, 'steps': steps, 'error': error}

    run_dir = ctx['run_dir']
    log = ctx.get('log') or (lambda m: None)
    try:
        manifest = _read_json(os.path.join(run_dir, '.embody',
                                           'manifest.json'))
        if not manifest:
            return {'ok': False, 'steps': steps,
                    'error': 'no .embody/manifest.json under %s -- nothing '
                             'was installed, so nothing to uninstall'
                             % run_dir}

        ok, detail = confirm_target(ctx)
        if not step('leg.target', ok, detail):
            return done('the smoke port does not serve the run dir -- '
                        'refusing to plant or uninstall anything')

        planted = plant_user_content(run_dir)
        step('plant_user_content', not planted['problems'],
             '; '.join(planted['notes']
                       + ['%d hashed' % len(planted['hashes'])]
                       + planted['problems']))

        before = repo_witness_hashes(ctx.get('repo'))
        present = sum(1 for v in before.values() if v)
        step('repo_baseline', present >= 2 or not ctx.get('repo'),
             '%d repo file(s) hashed in %s' % (present, ctx.get('repo'))
             if ctx.get('repo') else 'no repo in ctx -- fence not available')

        wiring = button_wiring(ctx)
        step('uninstall_button_wired',
             wiring.get('style') == 'Pulse' and wiring.get('routers'),
             'Uninstall par style=%r routed to uninstallHandler by %s'
             % (wiring.get('style'), wiring.get('routers') or 'NOTHING'))

        plan = json.loads(str(ctx['py'](
            'import json\nresult = json.dumps(op.Embody.PreviewUninstall())')))
        deletes = _plan_paths(plan, 'delete')
        strips = _plan_paths(plan, 'strip')
        reviews = _plan_paths(plan, 'review')
        step('preview_plan', bool(deletes or strips),
             'root=%s sources=%s delete=%d strip=%d review=%d unset=%d'
             % (plan.get('root'), plan.get('sources'), len(deletes),
                len(strips), len(reviews), len(plan.get('unset', []))))
        # The fence, taken BEFORE anything is executed: a plan rooted
        # anywhere but the run dir would delete another project's config.
        if not step('plan_root_is_run_dir',
                    _same_dir(plan.get('root') or '', run_dir),
                    'plan root %r vs run dir %r'
                    % (plan.get('root'), run_dir)):
            return done('plan root is not the run dir -- refusing to run '
                        'Uninstall')

        recorded, venv_rel = manifest_expectations(run_dir, manifest)
        accounted = (deletes | strips | reviews
                     | {_norm(p) for p in plan.get('missing', [])})
        missed = sorted(recorded - accounted)
        step('plan_covers_manifest', not missed,
             '%d recorded path(s) all accounted for' % len(recorded)
             if not missed else 'MISSING from the plan: %s' % missed[:8])

        wanted = ([venv_rel] if venv_rel else []) + ['.embody']
        wholesale = [rel for rel in wanted if rel not in deletes]
        step('plan_removes_state_and_venv', not wholesale,
             'venv=%s and .embody planned for removal' % venv_rel
             if not wholesale else 'not planned for removal: %s' % wholesale)

        listed = (deletes | strips | reviews) & {_norm(USER_FILE),
                                                 _norm(USER_RULE)}
        step('plan_spares_user_files', not listed,
             'user content absent from every plan bucket' if not listed
             else 'planned: %s' % sorted(listed))

        keepers = survivors(run_dir, plan)
        state_before = state_files(run_dir)
        log('uninstall: plan has %d removals; %d path(s) must survive'
            % (len(deletes), len(keepers)))

        script = uninstall_script()
        step('leaves_td_running', 'quit' not in script,
             'the leg never quits TD; Envoy dies with the uninstall, so '
             'teardown falls from its MCP rung to the pid-scoped '
             'close/force ladder (smoke_run.quit_smoke_td)')

        defer(ctx, script)
        wait_s = max(5.0, min(float(SUMMARY_TIMEOUT_S),
                              ctx['budget']() - TEARDOWN_RESERVE_S))
        text, arrived = ctx['wait_for_flag'](
            SUMMARY_FILE, wait_s, lambda t: t.strip().endswith('}'))
        summary = {}
        if arrived:
            try:
                summary = json.loads(text)
            except ValueError as e:
                summary = {'error': 'unparsable summary: %s' % e}
        step('uninstall_ran',
             arrived and summary.get('ran') and not summary.get('error'),
             json.dumps(summary) if arrived else
             'no %s in %s within %.0fs (text=%r)'
             % (SUMMARY_FILE, run_dir, wait_s, text))

        # Seams: real clock and sleep in the smoke, injected by the tests
        # (the same ctx['_seams'] the faults leg takes).
        seams = ctx.get('_seams') or {}
        step('envoy_stopped',
             envoy_is_down(ctx, clock=seams.get('clock'),
                           sleep=seams.get('sleep')),
             'port %s stopped answering after Uninstall' % ctx.get('port'))

        # Reported in the plan's own spelling: the normalized sets above are
        # for membership only, and a case-folded path reads as a typo.
        left = [e.get('path', '') for e in plan.get('delete', [])
                if os.path.exists(os.path.join(run_dir, e.get('path', '')))]
        # An 'emptydir' is a bare rmdir by design: it survives when the
        # user's own files are still in it, which is the point of the
        # planted rule file.
        empty_dirs = {e.get('path', '') for e in plan.get('delete', [])
                      if e.get('kind') == 'emptydir'}
        reborn = {_norm(p) for p in REBORN}
        stubborn = [p for p in left
                    if p not in empty_dirs and _norm(p) not in reborn]
        step('generated_removed', not stubborn,
             '%d/%d planned removals gone; kept dirs %s'
             % (len(plan.get('delete', [])) - len(left),
                len(plan.get('delete', [])),
                sorted(p for p in left if p in empty_dirs))
             + ('; STILL PRESENT %s' % stubborn[:8] if stubborn else ''))

        changed = [rel for rel in (USER_FILE, USER_RULE)
                   if planted['planted'].get(rel)
                   and _sha256(os.path.join(run_dir, rel))
                   != planted['hashes'].get(rel)]
        step('user_files_intact', not changed,
             'byte-identical: %s' % ', '.join(
                 r for r in (USER_FILE, USER_RULE)
                 if planted['planted'].get(r))
             if not changed else 'altered or removed: %s' % changed)

        mcp = _read_json(os.path.join(run_dir, '.mcp.json'))
        servers = (mcp or {}).get('mcpServers', {})
        step('mcp_json_reversed',
             planted['planted'].get('.mcp.json') and bool(mcp)
             and USER_SERVER in servers and 'envoy' not in servers,
             'mcpServers=%s' % sorted(servers) if mcp else
             '.mcp.json is gone -- the user server was deleted with it')

        kept_reviews = [e.get('path') for e in plan.get('review', [])
                        if not os.path.exists(
                            os.path.join(run_dir, e.get('path', '')))]
        step('review_items_kept', not kept_reviews,
             '%d flagged item(s) left untouched' % len(plan.get('review', []))
             if not kept_reviews else 'removed from review: %s'
             % kept_reviews[:8])

        if planted['planted'].get(SETTINGS_REL):
            settings = _read_json(os.path.join(run_dir, SETTINGS_REL))
            kept = (settings or {}).get(USER_SETTING) == 'keep-me'
            same = (_sha256(os.path.join(run_dir, SETTINGS_REL))
                    == planted['hashes'].get(SETTINGS_REL))
            # Stated, not asserted: the file is 'review' (embody_admin), so
            # Uninstall keeps it WHOLE -- its envoy permission entries
            # survive with the user's keys. Only include_review=True removes
            # it, and that would take the user's edits too.
            residue = 'envoy' in json.dumps(settings or {})
            step('settings_local_kept', kept and same,
                 'review bucket: byte-identical, %s kept%s'
                 % (USER_SETTING,
                    '; still names envoy (kept by design)' if residue else '')
                 if kept and same else
                 'user setting kept=%s byte-identical=%s' % (kept, same))

        if venv_rel:
            gone = not os.path.exists(os.path.join(run_dir, venv_rel))
            step('venv_removed', gone,
                 '%s removed (documented: Uninstall deletes Embody\'s .venv)'
                 % venv_rel if gone else '%s still present' % venv_rel)

        state_after = state_files(run_dir)
        survived_state = [f for f in state_after if f in state_before
                          and _norm(f) not in reborn]
        came_back = [f for f in state_after if f not in survived_state]
        step('embody_state_removed', not survived_state,
             '%d state file(s) removed%s' % (
                 len(state_before),
                 '; re-created by the post-uninstall settings save: %s'
                 % came_back if came_back else '')
             if not survived_state
             else 'still present: %s' % survived_state[:8])

        lost = [p for p in keepers
                if not os.path.exists(os.path.join(run_dir, p))]
        step('untouched_paths_kept', not lost,
             '%d unplanned path(s) survived (externalized .tdxn/.tox, the '
             'tracking table, .toe, logs)' % len(keepers)
             if not lost else 'removed but never planned: %s' % lost[:8])

        after = repo_witness_hashes(ctx.get('repo'))
        drift = [rel for rel in after if after[rel] != before.get(rel)]
        step('outside_run_dir_untouched', not drift,
             '%d repo file(s) unchanged' % len(after) if not drift
             else 'CHANGED in %s: %s' % (ctx.get('repo'), drift))

        return done()
    except Exception as e:
        return {'ok': False, 'steps': steps,
                'error': '%s: %s' % (type(e).__name__, e)}
