"""
Smoke test bootstrap - execute DAT callbacks for the template .toe.

This script goes into a text DAT (named 'execute', extension .py, callbacks
enabled) inside the smoke test template project. When the template .toe opens:

  1. onStart() fires at frame 0
  2. Frame 1: load the release .tox into the project root
  3. Frame 3: seed _smoke_test_responses on the new Embody COMP
  4. Embody's onCreate() sequence runs (frames 0-75 relative to loadTox)
  5. Dialogs at frame ~30 are auto-responded by _messageBox
  6. Envoy starts -> bridge reconnects -> MCP tools become available
  7. Verification checks run via MCP from the orchestrator

HOW TO RUN IT (isolated -- the only safe way):

    Copy the template OUT of the repo first, then launch it there:

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
  - Frame 80:  _write_ready_flag() - signals orchestrator that init is done
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
    repo_root = os.environ.get('EMBODY_SMOKE_REPO')
    if repo_root and os.path.isdir(repo_root):
        repo_root = os.path.normpath(repo_root)
    else:
        repo_root = os.path.normpath(os.path.join(project.folder, '..', '..'))
    me.store('repo_root', repo_root)

    # Clean up artifacts from previous runs - keep only the .toe and .py
    keep = {'.toe', '.py'}
    failed = []
    for entry in os.listdir(project.folder):
        if any(entry.endswith(ext) for ext in keep):
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

    # Find the latest release .tox
    tox_path = _find_latest_release_tox(repo_root)
    if not tox_path:
        _log('ERROR: No release .tox found in release/')
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

def _find_latest_release_tox(repo_root):
    """Find the newest Embody-v*.tox in the release/ directory."""
    import os, glob
    pattern = os.path.join(repo_root, 'release', 'Embody-v*.tox')
    candidates = sorted(glob.glob(pattern))
    return candidates[-1] if candidates else None


def _load_release_tox(tox_path=None):
    """Load the release .tox into the project root."""
    import os
    tox_path = tox_path or me.fetch('tox_path', None, search=False)
    if not tox_path or not os.path.isfile(tox_path):
        _log(f'ERROR: .tox not found at {tox_path}')
        return

    _log(f'Loading {os.path.basename(tox_path)}...')

    # Destroy any stale Embody from a previous test run saved in the .toe
    existing = op('/Embody')
    if existing:
        _log(f'Destroying stale Embody at {existing.path}')
        existing.destroy()

    # Load the .tox - creates the Embody COMP and triggers onCreate()
    embody = op('/').loadTox(tox_path)
    if not embody:
        _log('ERROR: loadTox returned None')
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

    # Auto-respond to all init dialogs:
    #   - Duplicate instance check: 'Ok' (button 0) - shouldn't fire in fresh project
    #   - Envoy opt-in: 'Enable Envoy' (button 1)
    # (The old Skip/Re-scan upgrade prompt is gone -- the upgrade path now
    #  validates quietly via _validateTrackedOperators; 'Embody': 0 remains
    #  for the duplicate-instance dialog, which shares that title.)
    responses = {
        'Embody': 0,
        'Embody - AI Coding Assistant Integration': 1,
        'Envoy \u2014 Git Repository Recommended': 3,  # 'Start Without Git'
    }
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
    here = os.path.normpath(project.folder)
    if os.path.normpath(here).lower().startswith(
            os.path.normpath(repo_root).lower()):
        _log('WARNING: smoke project runs INSIDE the Embody repo (%s). Its '
             'AI config deploys to the repo root and will overwrite the dev '
             'session\'s .mcp.json. Copy the template to a temp dir and set '
             'EMBODY_SMOKE_REPO to run a truly isolated smoke.' % here)


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
    return True, status


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

    MAX_ATTEMPTS = 40          # ~40 * 60 frames ~= 40s at 60fps
    if embody is not None:
        settled, envoy_status = _envoy_settled(embody)
        if not settled and attempt < MAX_ATTEMPTS:
            run("args[0](args[1])", _write_ready_flag, attempt + 1,
                delayFrames=60)
            return
    else:
        envoy_status = 'NO_EMBODY'

    flag_path = os.path.join(repo_root, 'dev', 'release_testing', 'ready.flag')
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
        verdict = 'PASS' if not problems else 'FAIL'

        with open(flag_path, 'w') as f:
            f.write(f'verdict={verdict}\n')
            f.write(f'problems={"; ".join(problems) if problems else "none"}\n')
            f.write(f'version={version}\n')
            f.write(f'status={status}\n')
            f.write(f'envoy_enabled={enabled}\n')
            f.write(f'envoy_status={envoy_status}\n')
            f.write(f'updatestatus={upd}\n')
            f.write(f'autosavestatus={autosave}\n')
            f.write(f'filecleanup={filecleanup}\n')
            f.write(f'script_errors={errors}\n')
            f.write(f'embody_path={embody_path}\n')
            f.write(f'settled_after_attempts={attempt}\n')
        _log(f'Ready flag written ({verdict}) to {flag_path}')
        if problems:
            _log(f'SMOKE FAIL: {"; ".join(problems)}')
    except Exception as e:
        _log(f'ERROR writing ready flag: {e}')


def _log(msg):
    """Print to textport with a prefix."""
    print(f'[smoke-test] {msg}')
