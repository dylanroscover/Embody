# me - this DAT
#
# frame - the current frame
# state - True if the timeline is paused
#
# Make sure the corresponding toggle is enabled in the Execute DAT.

import re
from pathlib import Path

comp = op.Embody
root = Path(project.folder).parents[0]

# TD build pattern, e.g. 2025.32820. Only ever substituted on ANCHORED lines
# (startswith match below) -- never file-wide -- so changelog/version-history
# mentions of older builds are left alone.
BUILD_RE = re.compile(r'\b\d{4}\.\d{3,6}\b')


def version(version):
    # get current versions
    increment = int(version.rsplit('.', 1)[1])
    major_minor = version.rsplit('.', 1)[0]
    # update version
    increment += 1
    new_version = f"{major_minor}.{increment}"
    comp.par.Version.val = new_version
    return new_version


def _rewriteText(text, transforms):
    """Pure line rewriter: transforms is [(startswith_anchor, fn), ...] where
    fn(line) -> new line. The first matching anchor wins per line. Returns
    (new_text, changed)."""
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        for anchor, fn in transforms:
            if line.startswith(anchor):
                new_line = fn(line)
                if new_line != line:
                    lines[i] = new_line
                    changed = True
                break
    return ''.join(lines), changed


def _rewriteFile(path, transforms):
    """Apply _rewriteText to a file in place. UTF-8 pinned (README contains
    emoji; locale-default codecs crash on Windows). Returns True if written."""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    new_text, changed = _rewriteText(text, transforms)
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_text)
    return changed


def updateVersionDocs(build, new_version):
    """Keep every user-facing version / minimum-build statement in lock-step
    with this save. The build we save with IS the minimum supported build
    (TD files do not open in older builds), so README, docs/index.md, and
    CONTRIBUTING.md restate the running build; the README badges restate
    par.Version and the build's year. Each file is guarded so a missing or
    unwritable doc can never abort a project save."""
    year = str(build).split('.')[0]
    targets = [
        (root / 'README.md', [
            ('[![Version](https://img.shields.io/badge/version-',
             lambda l: re.sub(r'version-[0-9][0-9.]*-', f'version-{new_version}-', l, count=1)),
            ('[![TouchDesigner](https://img.shields.io/badge/TouchDesigner-',
             lambda l: re.sub(r'TouchDesigner-\d{4}-', f'TouchDesigner-{year}-', l, count=1)),
            ('**Requirements:** TouchDesigner',
             lambda l: BUILD_RE.sub(build, l, count=1)),
        ]),
        (root / 'docs' / 'index.md', [
            ('- **TouchDesigner ',
             lambda l: BUILD_RE.sub(build, l, count=1)),
        ]),
        (root / 'CONTRIBUTING.md', [
            ('- **TouchDesigner ',
             lambda l: BUILD_RE.sub(build, l, count=1)),
        ]),
    ]
    for path, transforms in targets:
        try:
            _rewriteFile(path, transforms)
        except Exception as e:
            debug(f'version-doc bump skipped for {path.name}: {e}')


def onStart():
    return

def onCreate():
    return

def onExit():
    return

def onFrameStart(frame):
    return

def onFrameEnd(frame):
    return

def onPlayStateChange(state):
    return

def onDeviceChange():
    return

def onProjectPreSave():
    # set page for component
    comp.currentPage = 'Embody'

    # app.build, not project.saveBuild: pre-save, saveBuild still reports the
    # PREVIOUS save's build, so after a TD upgrade it would understate the
    # floor. The running build is what this save writes.
    build = str(app.build)
    old_version = comp.par.Version.val
    new_version = version(old_version)

    # version up (dev): README badges + minimum-build statements in
    # README / docs/index.md / CONTRIBUTING.md
    updateVersionDocs(build, new_version)

    # update build
    comp.par.Touchbuild = build

    # try to delete last release
    try:
        old_release = Path(project.folder).parents[0] / 'release' / f"{comp.name}-v{old_version}.tox"
        old_release.unlink()
    except Exception as e:
        # You might want to log the exception for debugging purposes
        # print(f"Error deleting old release: {e}")
        pass

    # Clear TDN UI pars so the baked .tox doesn't carry stale paths
    comp.par.Tdnfile = ''
    comp.par.Networkpath = ''

    # save out self-contained portable .tox (strips external file references)
    save_path = Path(project.folder).parents[0] / 'release' / f"{comp.name}-v{new_version}.tox"
    comp.ExportPortableTox(save_path=str(save_path))

    # Release manifest for the self-updater (UpdaterExt): version, TD-build
    # floor, and sha256 of the exported tox. Attached to GitHub releases
    # alongside the tox (the github-release rule globs release/*), it lets
    # the updater gate on min_td_build BEFORE downloading (an older build
    # loading a newer-build tox fails SILENTLY -- loadTox returns None) and
    # verify download integrity (release assets are mutable post-publish).
    writeReleaseManifest(comp, save_path, new_version, build)


def writeReleaseManifest(comp, tox_path, version, build):
    """Write release/embody-release.json describing the exported tox."""
    import hashlib
    import json
    from datetime import datetime, timezone
    try:
        payload = Path(tox_path).read_bytes()
        manifest = {
            'schema': 1,
            'name': comp.name,
            'version': version,
            'tag': f'v{version}',
            'asset': Path(tox_path).name,
            'size': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
            'td_build': build,
            'min_td_build': build,
            'build': int(comp.par.Build.eval()),
            'date': str(comp.par.Date.eval()),
            'exported_at': datetime.now(timezone.utc).isoformat(
                timespec='seconds'),
        }
        manifest_path = Path(tox_path).parent / 'embody-release.json'
        tmp = Path(str(manifest_path) + '.tmp')
        tmp.write_text(json.dumps(manifest, indent=1) + '\n',
                       encoding='utf-8')
        import os
        os.replace(str(tmp), str(manifest_path))
    except Exception as e:
        # A failed manifest must not block the save; the updater refuses
        # manifest-less releases loudly on its own.
        debug(f'[execute_src_ctrl] release manifest write failed: {e!r}')

def onProjectPostSave():
    return
