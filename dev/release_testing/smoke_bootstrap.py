"""
Smoke test bootstrap — execute DAT callbacks for the template .toe.

This script goes into a text DAT (named 'execute', extension .py, callbacks
enabled) inside the smoke test template project. When the template .toe opens:

  1. onStart() fires at frame 0
  2. Frame 1: load the release .tox into the project root
  3. Frame 3: seed _smoke_test_responses on the new Embody COMP
  4. Embody's onCreate() sequence runs (frames 0–75 relative to loadTox)
  5. Dialogs at frame ~30 are auto-responded by _messageBox
  6. Envoy starts → bridge reconnects → MCP tools become available
  7. Verification checks run via MCP from the orchestrator

Bootstrap timing (relative to onStart at frame 0):
  - Frame 1:  _load_release_tox() — creates Embody COMP, triggers onCreate
  - Frame 3:  _seed_responses() — before Verify() dialog at frame ~31
  - Frame 31: Verify() fires — auto-responded via _messageBox
  - Frame 41+: _promptEnvoy() fires — auto-responded via _messageBox
  - Frame 60+: Envoy starts, bridge reconnects
  - Frame 80:  _write_ready_flag() — signals orchestrator that init is done
"""

# me - this DAT
# frame - the current frame
# state - True if the timeline is paused


def onStart():
    """Project opened — kick off the smoke test bootstrap sequence."""
    import os, shutil
    # Discover repo root: template .toe lives in dev/release_testing/
    # so repo root is two levels up from project.folder
    repo_root = os.path.normpath(os.path.join(project.folder, '..', '..'))
    me.store('repo_root', repo_root)

    # Clean up artifacts from previous runs — keep only the .toe and .py
    keep = {'.toe', '.py'}
    for entry in os.listdir(project.folder):
        if any(entry.endswith(ext) for ext in keep):
            continue
        path = os.path.join(project.folder, entry)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception:
            pass
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

    # Load the .tox — creates the Embody COMP and triggers onCreate()
    embody = op('/').loadTox(tox_path)
    if not embody:
        _log('ERROR: loadTox returned None')
        return

    me.store('embody_path', embody.path)
    _log(f'Loaded Embody at {embody.path}')

    # Frame 3: seed auto-responses (before Verify dialog at ~frame 31)
    run("args[0]()", _seed_responses, delayFrames=2)

    # Frame 120: write ready flag — after Envoy has started (~frame 65)
    run("args[0]()", _write_ready_flag, delayFrames=119)


def _seed_responses():
    """Seed _smoke_test_responses so init dialogs are auto-answered."""
    embody_path = me.fetch('embody_path', None, search=False)
    embody = op(embody_path) if embody_path else None
    if not embody:
        _log('ERROR: Cannot find Embody COMP for response seeding')
        return

    # Auto-respond to all init dialogs:
    #   - Duplicate instance check: 'Ok' (button 0) — shouldn't fire in fresh project
    #   - Re-scan prompt: 'Skip' (button 0) — shouldn't fire in fresh install
    #   - Envoy opt-in: 'Enable Envoy' (button 1)
    responses = {
        'Embody': 0,
        'Embody - AI Coding Assistant Integration': 1,
        'Envoy \u2014 Git Repository Recommended': 3,  # 'Start Without Git'
    }
    embody.store('_smoke_test_responses', responses)
    _log(f'Seeded {len(responses)} auto-responses on {embody.path}')


def _write_ready_flag():
    """Write a ready flag file so the orchestrator knows init is done."""
    import os
    repo_root = me.fetch('repo_root', None, search=False)
    if not repo_root:
        return
    flag_path = os.path.join(repo_root, 'dev', 'release_testing', 'ready.flag')
    try:
        with open(flag_path, 'w') as f:
            embody_path = me.fetch('embody_path', '/Embody')
            embody = op(embody_path)
            status = embody.par.Status.eval() if embody else 'NOT_FOUND'
            envoy = embody.par.Envoyenable.eval() if embody else False
            errors = str(embody.scriptErrors) if embody else 'N/A'
            f.write(f'status={status}\n')
            f.write(f'envoy_enabled={envoy}\n')
            f.write(f'script_errors={errors}\n')
            f.write(f'embody_path={embody_path}\n')
            f.write(f'dev_toe={os.path.normpath(os.path.join(repo_root, "dev", "Embody-5.toe"))}\n')
        _log(f'Ready flag written to {flag_path}')
    except Exception as e:
        _log(f'ERROR writing ready flag: {e}')


def _log(msg):
    """Print to textport with a prefix."""
    print(f'[smoke-test] {msg}')
