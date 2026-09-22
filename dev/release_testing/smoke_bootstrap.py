r"""
Smoke test bootstrap - execute DAT callbacks for the template .toe.

This script goes into a text DAT (named 'execute', extension .py, callbacks
enabled) inside the smoke test template project. When the template .toe opens:

  1. onStart() fires at frame 0
  2. Frame 1: load the release .tox into the project root
  3. Frame 3: seed _smoke_test_responses on the new Embody COMP
  4. Embody's onCreate() sequence runs (frames 0-75 relative to loadTox)
  5. Dialogs at frame ~30 are auto-responded by _messageBox
  6. Envoy starts -> bridge reconnects -> MCP tools become available
  7. ready.flag verdict (startup health) -> on PASS, the project is saved
     (opens Embody's on-disk write gate) and the FEATURE phase runs:
     TDN round-trip, auto-save checkpoint, portable export, status
     readout, Envoy config, and a real Convoy enable-to-Connected --
     one verdict per feature in features.flag
  8. The MCP surface is probed separately from the orchestrator over
     HTTP (tools/list + create/set/query/read_tdn/errors/delete)

HOW TO RUN IT (isolated -- the only safe way):

    The orchestrator does all of this, on Windows and macOS, and reads
    the verdicts back:

        python dev/release_testing/smoke_run.py

    It stages a fresh per-run directory holding the template, this script
    and a `smoke_run.json` sidecar (repo_root, tox_path, flags_dir,
    run_id, platform). The sidecar is how this script learns where the
    repo is: an environment variable cannot be relied on, because macOS
    `open` launches through LaunchServices, which drops the shell's
    environment. EMBODY_SMOKE_REPO still works for a hand-run:

        mkdir %TEMP%\embody_smoke
        copy dev\release_testing\smoke_template.toe %TEMP%\embody_smoke\
        copy dev\release_testing\smoke_bootstrap.py %TEMP%\embody_smoke\
        set EMBODY_SMOKE_REPO=C:\path\to\Embody
        TouchDesigner.exe %TEMP%\embody_smoke\smoke_template.toe

    Running the template IN PLACE (from dev/release_testing/) puts the smoke
    instance's git root at the Embody repo, so Embody deploys its AI config
    into the repo -- overwriting the dev session's .mcp.json and repointing
    its bridge at the smoke instance's port. Retargeting Aiprojectroot is NOT
    the fix either: it migrates the config and cleans the old root, which is
    correct and marker-guarded (user files are never touched), but the Embody
    SOURCE repo is the one project whose Embody-generated files are committed
    to git -- so on 2026-07-27 that removed 16 marker-stamped files plus 4
    runtime files from the repo. Isolation has to come from the working
    directory. In-place runs still work and are warned about, but they are
    not a virgin install.

Bootstrap timing (relative to onStart at frame 0):
  - Frame 1:  _load_release_tox() - creates Embody COMP, triggers onCreate
  - Frame 3:  _seed_responses() - before Verify() dialog at frame ~31
  - Frame 31: Verify() fires - auto-responded via _messageBox
  - Frame 41+: _promptEnvoy() fires - auto-responded via _messageBox
  - Frame 60+: Envoy starts, bridge reconnects
  - Frame 120+: _write_ready_flag() polls Envoy to a terminal state, then
    writes the verdict - signals the orchestrator that init is done
  - PASS + ~30f: _save_then_exercise() saves the project (explicit path,
    no increment), waits out the 120-frame save window, then
    _exercise_features() writes features.flag leg by leg; the Convoy leg
    polls to Connected/timeout (bounded, ~3 min)
"""

# me - this DAT
# frame - the current frame
# state - True if the timeline is paused

def onStart():
    """Project opened - kick off the smoke test bootstrap sequence."""
    import os, shutil
    # Repo root: EMBODY_SMOKE_REPO when the template was copied elsewhere to
    # run isolated (the recommended path -- see the module docstring),
    # otherwise two levels up, which holds only when running in place from
    # dev/release_testing/.
    run_cfg = _read_run_config(project.folder)
    repo_root = _resolve_repo_root(
        [(run_cfg or {}).get('repo_root'), os.environ.get('EMBODY_SMOKE_REPO')],
        os.path.join(project.folder, '..', '..'))

    # A poisoned launch is detectable BEFORE the per-run reset below: this
    # storage key only ever exists in a mid-run save, so seeing it at
    # onStart means TD opened a stale incremental save (smoke_template.N.toe)
    # instead of the pristine template -- run 3 on 2026-08-10 did exactly
    # that and skipped the whole headless setup, so Envoy stayed Disabled.
    if me.fetch('headless_setup_done', False, search=False):
        _log('WARNING: this .toe carries mid-run state from a previous '
             'smoke (a stale incremental save was opened, not the pristine '
             'template) -- this run is NOT a virgin install')

    # Per-run reset: storage persists through a project save, so a saved
    # run would otherwise leak flags into the next one.
    for key in ('headless_setup_done', 'embody_path', 'tox_path',
                '_feature_results', 'flags_dir', 'run_id', 'run_platform',
                'envoy_reassert_done'):
        try:
            me.unstore(key)
        except Exception:
            pass
    me.store('repo_root', repo_root)
    me.store('flags_dir', _resolve_flags_dir(run_cfg, repo_root))
    me.store('run_id', str((run_cfg or {}).get('run_id') or ''))
    me.store('run_platform', str((run_cfg or {}).get('platform') or ''))

    # Clean up artifacts from previous runs - keep ONLY the pristine
    # template, the sidecar and the .py scripts. Notably NOT
    # smoke_template.N.toe: the feature phase saves the project, and a
    # leftover incremental save gets opened IN PLACE OF the pristine
    # template on the next launch, booting mid-run state (see the warning
    # above).
    failed = []
    for entry in os.listdir(project.folder):
        if entry in KEEP_NAMES or entry.endswith('.py'):
            continue
        path = os.path.join(project.folder, entry)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception as e:
            # Fail LOUD: a leftover .venv that cannot be deleted (file locks
            # from a zombie process) corrupts the Envoy bootstrap later --
            # observed 2026-07-25: locked pydantic_core .pyd -> venv corrupt
            # -> Envoy start aborted. Surface it at the START of the run.
            failed.append(f'{entry}: {e}')
    if failed:
        _log(f'WARNING: cleanup could NOT remove {len(failed)} entries '
             f'(locked?): {"; ".join(failed)} -- Envoy venv bootstrap may fail')
    else:
        _log('Cleaned test directory')

    # The release .tox: the sidecar's explicit path, else the one the
    # manifest names (never string order -- v6.2.9 sorts after v6.2.56).
    tox_path = _select_release_tox(repo_root, (run_cfg or {}).get('tox_path'))
    if not tox_path:
        wanted = (run_cfg or {}).get('tox_path') or os.path.join(
            repo_root, 'release', 'embody-release.json -> asset')
        _log(f'ERROR: release .tox not found ({wanted})')
        # Say so in the flag: an orchestrator would otherwise burn its
        # whole ready timeout and report only "no ready.flag".
        _write_abort_flag(f'release .tox not found: {wanted}')
        return

    me.store('tox_path', tox_path)
    _log(f'Bootstrap: will load {os.path.basename(tox_path)}')

    # Frame 1: load the .tox
    run("args[0]()", _load_release_tox, delayFrames=1)

def onCreate():
    pass

def onExit():
    pass

def onFrameStart(frame):
    pass

def onFrameEnd(frame):
    pass

def onPlayStateChange(state):
    pass

def onDeviceChange():
    pass

def onProjectPreSave():
    pass

def onProjectPostSave():
    pass

# =========================================================================
# Bootstrap helpers (not TD callbacks)
# =========================================================================

# Files the per-run cleanup sweep must leave alone (plus every *.py).
KEEP_NAMES = frozenset({'smoke_template.toe', 'smoke_run.json'})

# The seeded response store self-destructs when its last key is consumed,
# and an EMPTY store is what lets an unanticipated dialog fall through to a
# real, blocking ui.messageBox (EmbodyExt._messageBox). This key names no
# dialog, is never consumed, and keeps the store alive for the whole run --
# the unattended guarantee, made explicit (it used to rest on two dead
# keys by accident).
SMOKE_SENTINEL = '__smoke_sentinel__'

# Init dialogs, by their EXACT ui.messageBox titles (test_smoke_run.py
# checks each one against the source):
#   - 'Embody': the duplicate-instance dialog, 'Ok' (button 0); should not
#     fire in a fresh project
#   - Envoy opt-in: 'Enable Envoy' (button 1)
#   - the git prompt: 'Start Without Git' (button 3); the wizard path the
#     smoke drives never reaches it, so this is a belt-and-braces entry
SMOKE_RESPONSES = {
    'Embody': 0,
    'Embody - AI Coding Assistant Integration': 1,
    'Envoy -- Git Repository Recommended': 3,
    SMOKE_SENTINEL: 0,
}

# Seeded just before the Convoy leg enables Convoy (ConvoyExt titles).
SMOKE_CONVOY_RESPONSES = {
    'Embody - Enable Convoy': 1,
    'Embody - Upgrade Convoy Access': 1,
}


def _resolve_repo_root(candidates, fallback):
    """The first candidate that is a directory (sidecar, then
    EMBODY_SMOKE_REPO), else `fallback` -- two levels up, which only holds
    for an in-place run from dev/release_testing/."""
    import os
    for cand in candidates:
        if cand and os.path.isdir(str(cand)):
            return os.path.normpath(str(cand))
    return os.path.normpath(fallback)


def _write_atomic(path, text):
    """Write `text` to `path` via a sibling temp file + os.replace, so a
    reader polling the flag never sees a torn, half-written file."""
    import os
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _write_abort_flag(reason):
    """A FAIL ready.flag for an abort before Embody even loaded."""
    path = _flag_path('ready.flag')
    if not path:
        return
    try:
        _write_atomic(path, ''.join([
            'verdict=FAIL\n', f'problems={reason}\n', 'version=\n',
            'status=NOT_LOADED\n', 'envoy_enabled=False\n',
            'envoy_status=NOT_LOADED\n', 'updatestatus=\n',
            'autosavestatus=\n', 'filecleanup=\n', 'clipboardautopaste=\n',
            'convoy_enabled=False\n', 'convoy_status=\n', 'script_errors=\n',
            'embody_path=\n', 'settled_after_attempts=0\n',
            f'run_id={me.fetch("run_id", "", search=False)}\n',
            f'platform={me.fetch("run_platform", "", search=False)}\n',
            'tox=\n']))
        _write_features_flag({}, final=True)
    except Exception as e:
        _log(f'ERROR writing abort flag: {e}')


def _convoy_isolated():
    """Was Convoy pointed at a throwaway data directory for this run?
    The sidecar says so for an orchestrated run (smoke_run
    --isolate-convoy); EMBODY_CONVOY_DATA_DIR in TD's own environment
    covers a hand-run."""
    import os
    cfg = _read_run_config(project.folder) or {}
    return (cfg.get('convoy_isolated') == 'yes'
            or bool(os.environ.get('EMBODY_CONVOY_DATA_DIR')))


def _read_run_config(folder):
    """The orchestrator's `smoke_run.json` beside the template, or None
    (a hand-run has none; a broken file is treated as absent)."""
    import os, json
    path = os.path.join(folder, 'smoke_run.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    # Only non-empty strings count: a wrong-typed or blank value must fall
    # through to the next source, never be used as a path.
    return {k: v for k, v in cfg.items()
            if isinstance(v, str) and v.strip()}


def _resolve_flags_dir(run_cfg, repo_root):
    """Where ready.flag / features.flag go: the sidecar's `flags_dir` (the
    per-run directory, so two legs never write the same file), else the
    hand-run location inside the repo."""
    import os
    flags_dir = (run_cfg or {}).get('flags_dir')
    if flags_dir:
        return os.path.normpath(str(flags_dir))
    return os.path.join(repo_root, 'dev', 'release_testing')


def _flag_path(name):
    import os
    flags_dir = me.fetch('flags_dir', None, search=False)
    if not flags_dir:
        repo_root = me.fetch('repo_root', None, search=False)
        if not repo_root:
            return None
        flags_dir = os.path.join(repo_root, 'dev', 'release_testing')
    return os.path.join(flags_dir, name)


def _select_release_tox(repo_root, explicit=None):
    """The release .tox to smoke.

    `explicit` (the sidecar's tox_path) wins. Otherwise the asset the
    release manifest names -- the manifest is what the self-updater ships
    from, so it is the one file that is the release. A manifest that is
    present but unusable (unreadable, no asset, asset missing on disk)
    returns None rather than a different build. Only with no manifest at
    all does this fall back to the newest by NUMERIC version.
    """
    import os, re, json, glob
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    release_dir = os.path.join(repo_root, 'release')
    manifest = os.path.join(release_dir, 'embody-release.json')
    if os.path.isfile(manifest):
        try:
            with open(manifest, 'r', encoding='utf-8') as f:
                asset = json.load(f).get('asset')
        except Exception:
            return None
        if not asset:
            return None
        path = os.path.join(release_dir, str(asset))
        return path if os.path.isfile(path) else None
    candidates = glob.glob(os.path.join(release_dir, 'Embody-v*.tox'))

    def version_key(path):
        m = re.search(r'Embody-v(\d+(?:\.\d+)*)\.tox$', os.path.basename(path))
        return tuple(int(x) for x in m.group(1).split('.')) if m else ()

    candidates.sort(key=version_key)
    return candidates[-1] if candidates else None

def _load_release_tox(tox_path=None):
    """Load the release .tox into the project root."""
    import os
    tox_path = tox_path or me.fetch('tox_path', None, search=False)
    if not tox_path or not os.path.isfile(tox_path):
        _log(f'ERROR: .tox not found at {tox_path}')
        _write_abort_flag(f'.tox not found at {tox_path}')
        return

    _log(f'Loading {os.path.basename(tox_path)}...')

    # An exception here would go to the textport only, and the orchestrator
    # would wait its whole ready budget for a flag that never comes (first
    # CI run on the Mac, 2026-09-18: the log ended at "Destroying stale
    # Embody" with no trace of why). Log the traceback AND abort loudly.
    try:
        # Destroy any stale Embody from a previous test run saved in the .toe
        existing = op('/Embody')
        if existing:
            _log(f'Destroying stale Embody at {existing.path}')
            existing.destroy()

        # Load the .tox - creates the Embody COMP and triggers onCreate()
        embody = op('/').loadTox(tox_path)
    except Exception as e:
        import traceback
        _log('ERROR: loading the release .tox raised:\n'
             + traceback.format_exc())
        _write_abort_flag(f'loadTox raised {type(e).__name__}: {e}')
        return
    if not embody:
        _log('ERROR: loadTox returned None')
        _write_abort_flag('loadTox returned None')
        return

    me.store('embody_path', embody.path)
    _log(f'Loaded Embody at {embody.path}')

    # Frame 3: seed auto-responses (before Verify dialog at ~frame 31)
    run("args[0]()", _seed_responses, delayFrames=2)

    # Frame ~50: the Setup Wizard is a PANEL window, not a ui.messageBox, so
    # seeded responses cannot answer it and a headless smoke stalls on it
    # (observed 2026-07-29: Envoy stayed Disabled behind the wizard until a
    # human clicked). Drive the wizard's own backend instead.
    run("args[0]()", _apply_headless_setup, delayFrames=55)

    # Frame 120: write ready flag - after Envoy has started (~frame 65)
    run("args[0]()", _write_ready_flag, delayFrames=119)

def _apply_headless_setup(attempt=0):
    """Close the Setup Wizard (if open) and apply its Auto defaults directly.

    Mirrors a human clicking Auto -> Next -> ... -> Set up Embody: closes
    the wizard window, then calls the SAME backend entry point the wizard's
    finish() uses (_applyWizardSetup), with the choices a smoke wants --
    Auto mode, Claude Code assistant (exercises the Envoy enable + fresh
    venv bootstrap, the paths this smoke exists to verify), git skipped
    (temp folder), externalization skipped (the whole-project sweep is not
    smoke territory). The retry loop only covers the Embody COMP not
    existing yet; the wizard's own open is frame-scheduled (~onCreate+46),
    so this runs at +55 and a SECOND close fires at +150 to cover a
    late-slipping open (the wizard reopening over an already-applied setup
    is cosmetic, but the second close keeps the run clean). The setup
    apply itself happens exactly once.
    """
    if me.fetch('headless_setup_done', False):
        return
    embody_path = me.fetch('embody_path', None, search=False)
    embody = op(embody_path) if embody_path else None
    if not embody:
        if attempt < 10:
            run("args[0](args[1])", _apply_headless_setup, attempt + 1,
                delayFrames=30)
        return
    # Embody stores _init_complete once its settings restore has run. Apply
    # the setup BEFORE that and init's continuation overwrites it: the
    # wizard enables Envoy and it is Disabled two frames later, so the
    # install comes up dead (macOS CI 2026-09-20). delayFrames alone cannot
    # express this -- the frame init lands on moves with the machine.
    if not embody.fetch('_init_complete', False):
        if attempt < 40:
            run("args[0](args[1])", _apply_headless_setup, attempt + 1,
                delayFrames=15)
            return
        _log('WARNING: _init_complete never set; applying setup anyway')
    me.store('headless_setup_done', True)
    try:
        wizard_window = embody.op('window_wizard')
        if wizard_window is not None:
            wizard_window.par.winclose.pulse()
            _log('Closed the Setup Wizard window (headless smoke)')
    except Exception as e:
        _log(f'WARNING: could not close wizard window: {e}')
    try:
        embody.ext.Embody._applyWizardSetup(
            mode='auto', assistant='claudecode', client='', root='gitroot',
            custom_root='', permissions='all', git='gitskip',
            externalize='skip')
        _log('Applied headless wizard setup (auto/claudecode/skip-git)')
    except Exception as e:
        _log(f'ERROR: headless wizard setup failed: {e}')
    # Late-open cover: if the wizard's frame-scheduled open slipped past
    # this apply, close it again once, well after.
    run("args[0]()", _close_wizard_again, delayFrames=95)
    # And cover the stale-callback race: init() set Envoyenable=False to
    # stop an auto-start, TD defers that onValueChange, and parexec can
    # process it AFTER this enable -- Stop() then disables Envoy for the
    # whole run (execute.py:19-25 documents the hazard).
    run("args[0](args[1])", _reassert_envoy, 0, delayFrames=30)


def _reassert_envoy(attempt=0):
    """Put Envoy back if a deferred init callback turned it off after the
    setup enabled it. Checked several times: the callback's frame is not
    ours to predict."""
    embody_path = me.fetch('embody_path', None, search=False)
    embody = op(embody_path) if embody_path else None
    if not embody:
        return
    try:
        if not embody.par.Envoyenable.eval():
            embody.par.Envoyenable = True
            _log('Re-enabled Envoy: a deferred init callback had disabled it '
                 'after the headless setup (execute.py:19-25 race)')
    except Exception as e:
        _log(f'WARNING: could not re-assert Envoyenable: {e}')
        return
    if attempt < 6:
        run("args[0](args[1])", _reassert_envoy, attempt + 1, delayFrames=30)
    else:
        # The chain is done; a 'Disabled' from here on is the real answer.
        me.store('envoy_reassert_done', True)

def _close_wizard_again():
    embody_path = me.fetch('embody_path', None, search=False)
    embody = op(embody_path) if embody_path else None
    if not embody:
        return
    try:
        wizard_window = embody.op('window_wizard')
        if wizard_window is not None:
            wizard_window.par.winclose.pulse()
    except Exception:
        pass

def _seed_responses():
    """Seed _smoke_test_responses so init dialogs are auto-answered."""
    embody_path = me.fetch('embody_path', None, search=False)
    embody = op(embody_path) if embody_path else None
    if not embody:
        _log('ERROR: Cannot find Embody COMP for response seeding')
        return

    responses = dict(SMOKE_RESPONSES)  # see the constant for each title
    embody.store('_smoke_test_responses', responses)
    _log(f'Seeded {len(responses)} auto-responses on {embody.path}')

    # DO NOT touch Aiprojectroot here -- not because the parameter is unsafe,
    # but because THIS template lives inside the Embody repo. Changing the
    # AI-config root migrates the config and cleans the old root, which is
    # correct behaviour and is careful about it: only files carrying the
    # 'Generated by Embody' marker are unlinked, directories go through
    # rmdir() (fails on non-empty, so user content survives), and .mcp.json
    # loses only its 'envoy' entry. Run here on 2026-07-27 it did exactly
    # that -- swept .claude/skills/, removed the 14 GENERATED skills, left
    # the 7 hand-written dev-only ones and CLAUDE.md untouched.
    #
    # It still hurt, for a reason specific to this repo: the Embody SOURCE
    # repo is the one project where Embody-generated files are themselves
    # committed to git. A normal user project loses nothing -- everything is
    # regenerated at the new root.
    #
    # So isolation must come from WHERE the template runs, not from a
    # parameter -- see the module docstring: copy the template to a temp
    # directory and launch it there, so its git root is the temp dir and the
    # repo is never a candidate for either clobbering or cleanup.
    _check_running_inside_repo()

def _check_running_inside_repo():
    """Warn LOUDLY when the smoke project sits inside the Embody repo.

    A fresh-install smoke is supposed to model a virgin project. Run from
    inside the repo, the smoke instance's git root IS the repo, so Embody
    deploys its AI config there -- overwriting the dev session's .mcp.json
    and repointing its bridge at the smoke instance's port (observed
    2026-07-26). The fix is to run from a copy OUTSIDE the repo (see the
    module docstring), not to retarget Aiprojectroot, which deletes the
    repo's generated files instead (2026-07-27).
    """
    import os
    repo_root = me.fetch('repo_root', None, search=False)
    if not repo_root:
        return
    # realpath + normcase: macOS reports a $TMPDIR project as /private/var/...
    # while the repo path may be un-resolved, and .lower() alone is a
    # Windows-ism on a case-sensitive volume.
    here = os.path.normcase(os.path.realpath(project.folder))
    if here.startswith(os.path.normcase(os.path.realpath(repo_root))):
        _log('WARNING: smoke project runs INSIDE the Embody repo (%s). Its '
             'AI config deploys to the repo root and will overwrite the dev '
             'session\'s .mcp.json. Run dev/release_testing/smoke_run.py, '
             'which stages an isolated copy, instead.' % here)

def _envoy_settled(embody):
    """(settled, status) for the Envoy server.

    Envoy's first-run venv bootstrap takes ~20s+, far longer than the frame
    budget the ready flag used to fire on, so a flag written at frame ~120
    caught Envoy mid-install and reported the OPT-IN parameter
    (Envoyenable=True) as if it were server health. It read green while the
    server had actually aborted (observed 2026-07-26: locked .venv ->
    'Envoy start aborted -- dependency install failed'). Wait for a TERMINAL
    state instead of a frame count.
    """
    try:
        status = str(embody.par.Envoystatus.eval())
    except Exception as e:
        return True, f'UNREADABLE ({e})'
    # Embody's own init (catalog scan, validation sweep) delays the Envoy
    # opt-in, so Envoystatus reads a terminal-looking 'Disabled' BEFORE the
    # decision was ever offered. Observed 2026-07-29: the flag settled at
    # attempt 0 with Status='Scanning defaults (40/688)' and reported a
    # false FAIL. Embody Status != 'Enabled' means init is still in flight
    # (or genuinely wedged -- the attempt cap still bounds the wait and the
    # final flag then records the real state).
    try:
        embody_status = str(embody.par.Status.eval())
    except Exception:
        embody_status = ''
    if embody_status and embody_status != 'Enabled':
        return False, status
    lowered = status.lower()
    pending = ('starting', 'installing', 'warming', 'restarting', 'reviving')
    if any(tok in lowered for tok in pending):
        return False, status
    # 'Disabled' is 'not enabled YET' until the smoke's own setup has run
    # and the re-assert chain has finished: this harness enables Envoy
    # itself, and a deferred init callback can Stop() it moments after
    # (execute.py:19-25). A poll landing in either window used to settle
    # FAIL on a healthy install -- 'settled after 7 polls Disabled' on the
    # macOS runner. The attempt cap still bounds it, so an Envoy that is
    # genuinely off is still reported.
    if lowered.startswith('disabled') and not _envoy_decided():
        return False, status
    return True, status


def _envoy_decided() -> bool:
    """Has this harness finished having its say about Envoy?"""
    return bool(me.fetch('headless_setup_done', False, search=False)
                and me.fetch('envoy_reassert_done', False, search=False))

def _write_ready_flag(attempt=0):
    """Write the ready flag once Envoy reaches a terminal state.

    Re-schedules itself while Envoy is still coming up, up to a deadline.
    Writes a VERDICT so the orchestrator cannot mistake "opted in" for
    "running" -- the flag is the release gate's evidence, and a flag that
    reads green while Envoy failed is worse than no flag.
    """
    import os
    repo_root = me.fetch('repo_root', None, search=False)
    if not repo_root:
        return
    embody_path = me.fetch('embody_path', '/Embody')
    embody = op(embody_path)

    # ~N * 60 frames ~= N seconds at 60fps. The one-time venv bootstrap
    # (pip installing the whole MCP stack into a fresh project) is the
    # long pole and scales with how loaded the machine is: a 40s budget
    # produced a false FAIL -- "Envoy not running: 'Installing deps...
    # (one-time)'" at attempt 40 -- on a box that was simultaneously
    # running the test matrix, while the very same build settled in 20
    # attempts when idle (2026-08-06). The attempt cap exists to bound a
    # genuinely WEDGED start, not to race an install, so give it room:
    # a real wedge still reports, four minutes later, with the true state.
    MAX_ATTEMPTS = 240
    if embody is not None:
        settled, envoy_status = _envoy_settled(embody)
        if not settled and attempt < MAX_ATTEMPTS:
            run("args[0](args[1])", _write_ready_flag, attempt + 1,
                delayFrames=60)
            return
    else:
        envoy_status = 'NO_EMBODY'

    flag_path = _flag_path('ready.flag')
    try:
        def _par(name, default='NOT_FOUND'):
            try:
                return str(getattr(embody.par, name).eval())
            except Exception:
                return default

        status = _par('Status') if embody else 'NOT_FOUND'
        version = _par('Version') if embody else 'NOT_FOUND'
        enabled = _par('Envoyenable') if embody else 'False'
        errors = str(embody.scriptErrors()) if embody else 'N/A'
        # The fresh-install readouts the release procedure actually checks:
        # a release .tox bakes in whatever the dev session last set, and a
        # BLANK Updatestatus on a fresh install is the v6.0.145 regression.
        upd = _par('Updatestatus') if embody else 'NOT_FOUND'
        autosave = _par('Autosavestatus') if embody else 'NOT_FOUND'
        filecleanup = _par('Filecleanup') if embody else 'NOT_FOUND'
        # Clipboard auto-paste watcher: authored default On, and v6.0.251
        # shipped it baked Off (the dev machine's toggle at bake time --
        # the Showbuiltinpars leak class), killing the collection-page
        # "Embody it" copy flow on every fresh install. A fresh install
        # MUST come up On.
        clip = _par('Clipboardautopaste') if embody else 'NOT_FOUND'

        # CONVOY WAS NEVER CHECKED HERE, and that is exactly how a broken
        # fresh install shipped: on 2026-08-09 a clean v6.0.230 install on a
        # clean machine failed its Convoy host-app install outright (no
        # interpreter -- Envoy was still building the venv Convoy shares),
        # and this harness reported PASS because it only ever asked about
        # Embody and Envoy. A smoke that cannot see a whole subsystem fail
        # is worse than no smoke, because it is quoted as evidence.
        # There is no separate Host App parameter -- _hostStatus merges the
        # host-app line INTO Convoystatus (ConvoyExt._hostStatus /
        # _publishStatus), so that one readout carries both.
        convoy_enabled = _par('Convoyenable', 'NOT_FOUND') if embody else 'NOT_FOUND'
        convoy_status = _par('Convoystatus', 'NOT_FOUND') if embody else 'NOT_FOUND'
        convoy_on = str(convoy_enabled).lower() in ('1', 'true', 'on')

        problems = []
        if embody is None:
            problems.append('Embody COMP not found')
        if status != 'Enabled':
            problems.append(f'Status={status!r} (want Enabled)')
        if 'running' not in envoy_status.lower():
            problems.append(f'Envoy not running: {envoy_status!r}')
        if errors and errors not in ('', 'N/A'):
            problems.append(f'script errors: {errors[:120]}')
        if not upd or upd == 'NOT_FOUND':
            problems.append('Updatestatus is BLANK (v6.0.145 regression)')
        if str(clip).lower() not in ('1', 'true', 'on'):
            problems.append(
                f'Clipboardautopaste={clip!r} on a fresh install '
                f'(v6.0.251 baked-Off leak class; authored default is On)')
        if convoy_on:
            # Only assert when the user actually opted in -- Convoy off is a
            # legitimate fresh-install state and must not fail the smoke.
            bad = ('failed', 'error', 'no runtime', 'no interpreter',
                   'cannot start')
            blob = str(convoy_status).lower()
            if any(word in blob for word in bad):
                problems.append(
                    f'Convoy is enabled but its host app did not come '
                    f'up: Convoystatus={convoy_status!r}')
        verdict = 'PASS' if not problems else 'FAIL'

        # Run stamp (last three lines), so a reader can tell a fresh flag
        # from a leftover and which build a leg actually smoked. Written
        # atomically: a poll can never see a torn file.
        _write_atomic(flag_path, ''.join([
            f'verdict={verdict}\n',
            f'problems={"; ".join(problems) if problems else "none"}\n',
            f'version={version}\n',
            f'status={status}\n',
            f'envoy_enabled={enabled}\n',
            f'envoy_status={envoy_status}\n',
            f'updatestatus={upd}\n',
            f'autosavestatus={autosave}\n',
            f'filecleanup={filecleanup}\n',
            f'clipboardautopaste={clip}\n',
            f'convoy_enabled={convoy_enabled}\n',
            f'convoy_status={convoy_status}\n',
            f'script_errors={errors}\n',
            f'embody_path={embody_path}\n',
            f'settled_after_attempts={attempt}\n',
            f'run_id={me.fetch("run_id", "", search=False)}\n',
            f'platform={me.fetch("run_platform", "", search=False)}\n',
            f'tox={os.path.basename(str(me.fetch("tox_path", "", search=False)))}\n',
        ]))
        _log(f'Ready flag written ({verdict}) to {flag_path}')
        if problems:
            _log(f'SMOKE FAIL: {"; ".join(problems)}')
        # Startup green -> exercise the actual features (TDN round-trip,
        # checkpoint, portable export, status panel, Envoy config, Convoy
        # enable). Startup red -> write features.flag as all-SKIP so the
        # orchestrator sees an explicit "not reached", never a stale file.
        if verdict == 'PASS':
            run("args[0](args[1])", _save_then_exercise, 0, delayFrames=30)
        else:
            _write_features_flag({}, final=True)
    except Exception as e:
        _log(f'ERROR writing ready flag: {e}')

def _log(msg):
    """Print to textport with a prefix, and mirror into bootstrap.log in
    the flags dir: Embody's own file log only starts after the feature
    phase's save, so on a wedged startup this mirror is the only evidence
    an orchestrator can collect."""
    line = f'[smoke-test] {msg}'
    print(line)
    try:
        import os, time
        flags_dir = me.fetch('flags_dir', None, search=False)
        if flags_dir:
            with open(os.path.join(flags_dir, 'bootstrap.log'), 'a',
                      encoding='utf-8') as f:
                f.write(time.strftime('%H:%M:%S ') + line + '\n')
    except Exception:
        pass


# =========================================================================
# Feature exercise -- the smoke tests FEATURES, not just startup.
#
# The startup flag proved only that the app came up; a whole subsystem
# could be broken behind a green boot (and was: a clean v6.0.230 install
# failed its Convoy host-app install while the smoke printed PASS,
# because nothing here ever asked). This phase drives the real features
# in the virgin project -- TDN externalize/save/reimport round-trip, an
# auto-save checkpoint, the portable .tox export, the status readout,
# Envoy's generated config, and a full Convoy enable (consent seeded,
# host app installed and connected) -- and writes features.flag with one
# verdict per feature. The MCP surface itself is exercised from the
# orchestrator over HTTP, because an in-process check cannot prove the
# server answers.
# =========================================================================

FEATURE_ORDER = ('embody_core', 'tdn_roundtrip', 'autosave_checkpoint',
                 'portable_export', 'viz_status', 'envoy_config', 'convoy')

# Convoy needs to install + start a real background app and register:
# minutes on a slow machine, and the enable path deliberately WAITS for
# a runtime. 90 polls x 2 s is three minutes before an honest FAIL.
CONVOY_POLL_FRAMES = 120
CONVOY_POLL_MAX = 90


def _features_flag_text(results, final=False):
    """One line per feature, NAME=VERDICT|detail, in FEATURE_ORDER.

    A leg without a result is PENDING while the phase is still running
    (the file is rewritten after every leg, and a reader polling between
    two legs must keep waiting, not count the rest as failed) and SKIP
    only on the `final` write: the phase ended without reaching it.
    """
    filler = ('SKIP', 'not reached') if final else ('PENDING', 'not reached')
    return ''.join('%s=%s|%s\n' % (name, *results.get(name, filler))
                   for name in FEATURE_ORDER)


def _write_features_flag(results, final=False):
    """Rewritten atomically after every leg (see _features_flag_text)."""
    path = _flag_path('features.flag')
    if not path:
        return
    try:
        _write_atomic(path, _features_flag_text(results, final))
    except Exception as e:
        _log('ERROR writing features flag: %s' % (e,))


def _feature(results, name, fn):
    """Run one feature check; a failure is a verdict, never an abort."""
    try:
        detail = fn()
        results[name] = ('PASS', detail or 'ok')
        _log('FEATURE %s: PASS (%s)' % (name, detail))
    except Exception as e:
        import traceback
        tb = traceback.format_exc().strip().splitlines()
        where = tb[-2].strip() if len(tb) > 1 else ''
        # One line per leg: a newline inside the message would split the
        # flag line and could forge the next leg's verdict.
        detail = ('%s @ %s' % (e, where)).replace('\r', ' ').replace('\n', ' ')
        results[name] = ('FAIL', detail)
        _log('FEATURE %s: FAIL -- %s' % (name, e))
    # Persist after EVERY feature: a crash mid-run must still leave
    # per-feature evidence, not an absent flag the orchestrator times
    # out on.
    _write_features_flag(results)


def _save_then_exercise(attempt=0):
    """Give the fresh install its real home, then run the feature legs.

    The wizard's real flow ends in a project save, and every disk write
    Embody makes on its own initiative sits behind the on-disk gate: a
    file must exist at project.folder/project.name. On the pristine
    template copy that is FALSE -- TD reports the next incremental save
    name (smoke_template.4.toe) while the disk holds smoke_template.toe
    -- so externalization defers, Checkpoint refuses, and a Convoy
    enable reverts to 'Waiting for project save' (all observed on the
    first feature run, 2026-08-10). One real save mirrors the wizard
    and opens the gate; the settle loop covers the save's own
    strip/restore + extension reinit phases.
    """
    import os
    if attempt == 0:
        _log('Saving project -- feature legs need the on-disk gate open')
        try:
            # Explicit path = exactly the file Embody's gate checks
            # (project.folder/project.name). A bare save() auto-increments
            # to smoke_template.N.toe, and the next launch then opens that
            # increment instead of the pristine template (run 3,
            # 2026-08-10). The gate path never exists at this point (that
            # is WHY the gate is closed), so no overwrite prompt fires.
            project.save(os.path.join(project.folder, project.name))
        except Exception as e:
            _log('ERROR: project.save() raised: %s' % (e,))
    try:
        gate_open = os.path.isfile(
            os.path.join(project.folder, project.name))
    except Exception:
        gate_open = False
    if not gate_open and attempt < 10:
        run("args[0](args[1])", _save_then_exercise, attempt + 1,
            delayFrames=60)
        return
    if not gate_open:
        _log('WARNING: save did not open the on-disk gate; features will '
             'show the gated behavior')
    run("args[0]()", _exercise_features, delayFrames=60)


def _exercise_features(attempt=0):
    """Drive the real features, one verdict each. Runs after ready.flag."""
    import os
    embody = op(me.fetch('embody_path', '/Embody'))
    results = {}
    if embody is None:
        _write_features_flag(results)
        return
    # The save window outlives project.save() by 120 frames (execute.py
    # clears _suppress_dialogs on a delay), and Checkpoint correctly
    # refuses inside it -- run 2 hit exactly that boundary. Wait it out.
    if (embody.fetch('_suppress_dialogs', False, search=False)
            and attempt < 20):
        run("args[0](args[1])", _exercise_features, attempt + 1,
            delayFrames=30)
        return
    ext = embody.ext.Embody

    def core():
        assert str(embody.par.Status.eval()) == 'Enabled', \
            str(embody.par.Status.eval())
        table = ext.Externalizations
        assert table is not None and table.numRows >= 1
        return 'Status Enabled, table %d rows' % table.numRows

    def tdn():
        home = op('/')
        old = home.op('smoke_tdn')
        if old:
            old.destroy()
        comp = home.create(baseCOMP, 'smoke_tdn')
        comp.nodeX, comp.nodeY = 600, -400
        n = comp.create(noiseTOP, 'noise_src')
        n.nodeX, n.nodeY = 0, 0
        out = comp.create(nullTOP, 'null_out')
        out.nodeY = 0
        out.nodeX = n.nodeX + n.nodeWidth + 200
        out.inputConnectors[0].connect(n)
        n.par.period = 7.5
        ext.applyTagToOperator(comp, 'tdn')
        ext.externalizeImmediate(comp)
        rel = ext._getStrategyFilePath(comp.path, 'tdn')
        assert rel, ('no tracking row after ExternalizeImmediate -- '
                     'the on-disk save gate is closed?')
        path = str(ext.buildAbsolutePath(rel))
        assert os.path.isfile(path), 'no .tdn written at %s' % path
        text = open(path, encoding='utf-8').read()
        assert 'noise_src' in text and 'null_out' in text, \
            'ops missing from .tdn'
        # mutate -> save -> the file must carry the new value
        n.par.period = 9.25
        ext.saveTDXN(comp.path)
        text = open(path, encoding='utf-8').read()
        assert '9.25' in text, 'SaveTDN did not persist the change'
        # disk -> network: rebuild from the file, verify the value returns
        n.par.period = 1.0
        r = embody.ext.TDXN.importNetworkFromFile(path, comp.path,
                                                 clear_first=True)
        assert isinstance(r, dict), 'import returned %r' % (r,)
        back = comp.op('noise_src')
        assert back is not None, 'import lost noise_src'
        assert abs(float(back.par.period.eval()) - 9.25) < 1e-6, \
            'round-trip lost the value: %r' % back.par.period.eval()
        assert comp.op('null_out') is not None
        return 'externalize -> save -> reimport round-trip held 9.25'

    def autosave():
        comp = op('/smoke_tdn')
        assert comp is not None, 'tdn feature must run first'
        # The project.save() that opened the gate must ALSO have reset
        # the Saved counter -- a Ctrl+S is the strongest form of "work
        # reached disk", and until 2026-08-11 the readout kept the last
        # CHECKPOINT's age straight through it (this flag read 'Idle'
        # after the harness's own save, which is how the gap was proven).
        pre = str(embody.par.Autosavestatus.eval())
        assert pre.startswith('Saved'), \
            'post-save stamp missing: Autosavestatus=%r after save' % pre
        comp.op('noise_src').par.period = 4.5
        ok = ext.checkpoint(comp.path)
        assert ok, 'Checkpoint returned falsy'
        status = str(embody.par.Autosavestatus.eval())
        assert status.startswith('Saved'), 'Autosavestatus=%r' % status
        return 'save stamped %r; checkpoint wrote %r' % (pre, status)

    def portable():
        comp = op('/smoke_tdn')
        assert comp is not None
        # Forward slashes, project-local: a backslashed tempdir path made
        # the export land nowhere visible on the first run.
        dest = project.folder + '/smoke_portable.tox'
        if os.path.isfile(dest):
            os.remove(dest)
        ok = ext.ExportPortableTox(target=comp, save_path=dest)
        assert ok, 'ExportPortableTox returned falsy'
        assert os.path.isfile(dest), 'portable .tox missing'
        size = os.path.getsize(dest)
        # Size alone proves nothing -- load it back and count children.
        stale = op('/smoke_portable')
        if stale:
            stale.destroy()
        back = op('/').loadTox(dest)
        try:
            kids = sorted(c.name for c in back.children)
            assert 'noise_src' in kids and 'null_out' in kids, \
                'reloaded tox lost children: %r' % (kids,)
        finally:
            back.destroy()
            os.remove(dest)
        return 'portable .tox %d bytes, reload kept both children' % size

    def viz():
        viz_comp = embody.op('viz_status')
        assert viz_comp is not None, 'viz_status COMP missing'
        pub = viz_comp.op('status_publish')
        assert pub is not None, 'status_publish DAT missing'
        pub.module.Refresh()
        table = viz_comp.op('status_table')
        assert table is not None, 'status_table missing'
        # The publish contract: header + one row per table_rows() entry
        # (19 rows today -- PANEL_ROWS counts panel LINES, not table rows).
        mod = embody.op('startup_progress').module
        expected = mod.table_rows(embody, viz_comp.par.w.eval(),
                                  now=absTime.seconds)
        want_rows = len(expected) + 1
        assert table.numRows == want_rows, \
            'status_table has %d rows, want %d' % (table.numRows, want_rows)
        values = [str(table[r, 'value']) for r in range(1, table.numRows)]
        filled = [v for v in values if v.strip()]
        assert len(filled) >= 3, 'panel mostly blank: %r' % (values,)
        errs = viz_comp.errors(recurse=True) or ''
        assert not errs.strip(), 'viz errors: %s' % errs[:120]
        return 'status_table %d rows, first: %r' % (len(values), values[0])

    def envoy_cfg():
        folder = str(project.folder)
        assert os.path.isfile(os.path.join(folder, '.mcp.json')), \
            '.mcp.json not written'
        status = str(embody.par.Envoystatus.eval())
        assert status.startswith('Running on port'), 'Envoystatus=%r' % status
        return status

    _feature(results, 'embody_core', core)
    _feature(results, 'tdn_roundtrip', tdn)
    _feature(results, 'autosave_checkpoint', autosave)
    _feature(results, 'portable_export', portable)
    _feature(results, 'viz_status', viz)
    _feature(results, 'envoy_config', envoy_cfg)

    # Convoy last and asynchronous: consent is seeded (the dialog is the
    # real one -- this exercises the ACTUAL enable path, including the
    # wait-for-Envoy loop and the host-app install), then the status is
    # polled to a terminal answer. NOTE for whoever reads the flag: this
    # installs and starts the real per-user host app on this machine.
    results['convoy'] = ('PENDING', 'enable fired, polling')
    _write_features_flag(results)
    me.store('_feature_results', results)
    try:
        responses = embody.fetch('_smoke_test_responses', {}, search=False)
        responses.update(SMOKE_CONVOY_RESPONSES)
        responses.setdefault(SMOKE_SENTINEL, 0)
        embody.store('_smoke_test_responses', responses)
        embody.par.Convoyenable = True
        run('args[0](args[1])', _await_convoy, 0,
            delayFrames=CONVOY_POLL_FRAMES)
    except Exception as e:
        results['convoy'] = ('FAIL', 'enable raised: %s' % (e,))
        _write_features_flag(results, final=True)


def _await_convoy(attempt):
    """Poll Convoystatus to a terminal verdict. Bounded."""
    embody = op(me.fetch('embody_path', '/Embody'))
    results = me.fetch('_feature_results', {}, search=False)
    if embody is None:
        return
    status = str(embody.par.Convoystatus.eval())
    low = status.lower()
    if low.startswith('connected'):
        results['convoy'] = ('PASS', status)
        _write_features_flag(results, final=True)
        _log('FEATURE convoy: PASS (%s)' % status)
        return
    if _convoy_isolated() and low.startswith('no convoy host app'):
        # smoke_run --isolate-convoy: the enable path ran against an
        # ISOLATED data directory, where no login host app is installed
        # by design (ConvoyExt refuses). That is the terminal answer.
        results['convoy'] = ('PASS', 'isolated: %s' % status)
        _write_features_flag(results, final=True)
        _log('FEATURE convoy: PASS (isolated -- %s)' % status)
        return
    if low.startswith(('error', 'refused', 'install failed')):
        results['convoy'] = ('FAIL', status)
        _write_features_flag(results, final=True)
        _log('FEATURE convoy: FAIL -- %s' % status)
        return
    if attempt >= CONVOY_POLL_MAX:
        results['convoy'] = ('FAIL', 'timed out at: %s' % status)
        _write_features_flag(results, final=True)
        _log('FEATURE convoy: FAIL -- timed out at %r' % status)
        return
    results['convoy'] = ('PENDING', status)
    _write_features_flag(results)
    run('args[0](args[1])', _await_convoy, attempt + 1,
        delayFrames=CONVOY_POLL_FRAMES)
