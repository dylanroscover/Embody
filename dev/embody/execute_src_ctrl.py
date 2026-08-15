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
    (TD files do not open in older builds), so README, docs/index.md,
    CONTRIBUTING.md and the embody.tools site restate the running build; the
    README badges restate par.Version and the build's year. Each file is
    guarded so a missing or unwritable doc can never abort a project save."""
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
        # embody.tools (Astro). ONE constant feeds every requirement
        # statement on the site -- the landing page's "Requires
        # TouchDesigner ..." line and the SoftwareApplication JSON-LD --
        # so this single anchored line keeps the whole site in sync. It
        # went stale at 2025.32280 while the docs said 33070 precisely
        # because the site restated the build as a literal.
        (root / 'platform' / 'apps' / 'web' / 'src' / 'lib' / 'tdBuild.ts', [
            ('export const MIN_TD_BUILD',
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
    # NOTE: release hooks fire here if pre_release/post_release Text DATs
    # ever exist directly under the Embody COMP -- this export runs on
    # EVERY project.save via onProjectPreSave.
    save_path = Path(project.folder).parents[0] / 'release' / f"{comp.name}-v{new_version}.tox"
    ok = comp.ExportPortableTox(save_path=str(save_path))
    if not ok:
        # Three False cases: pre_release abort or save failure (no tox on
        # disk), or a post_release failure AFTER a successful save (tox
        # exists). Either way the manifest is NOT written, and the prior
        # version's manifest -- now pointing at the tox unlinked above --
        # is removed so nothing stale ships.
        if save_path.is_file():
            comp.Log(
                f"Release export for v{new_version} reported failure "
                f"AFTER writing the tox (post_release hook failure?) -- "
                f"manifest NOT written. Verify release/ before pushing.",
                "ERROR")
        else:
            comp.Log(
                f"Release export FAILED for v{new_version} (pre_release "
                f"hook abort or save failure) -- release/ has no tox for "
                f"this version and the manifest was NOT written. Fix and "
                f"re-save.", "ERROR")
        try:
            (Path(project.folder).parents[0] / 'release'
             / 'embody-release.json').unlink()
        except Exception:
            pass
        return

    # Release manifest for the self-updater (UpdaterExt): version, TD-build
    # floor, and sha256 of the exported tox. Attached to GitHub releases
    # alongside the tox (the /release skill's github-release procedure globs release/*), it lets
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
        # newline='' pins LF. Path.write_text goes through text mode, which on
        # Windows translates every \n to \r\n -- the manifest is fetched and
        # hashed as bytes by the updater, and .gitattributes normalization
        # hides the CRLF locally, so the drift is invisible right up until it
        # matters. (Same class the v6.0.168 fix closed for the other manifests.)
        with open(str(tmp), 'w', encoding='utf-8', newline='') as handle:
            handle.write(json.dumps(manifest, indent=1) + '\n')
        import os
        os.replace(str(tmp), str(manifest_path))
    except Exception as e:
        # A failed manifest must not block the save; the updater refuses
        # manifest-less releases loudly on its own.
        debug(f'[execute_src_ctrl] release manifest write failed: {e!r}')

# How many 5-frame waits the sync will spend on a TDN restore that has not
# finished before it gives up and says so. ~2.5s at 60fps: longer than any
# observed strip/restore, short enough that a stuck flag is reported inside
# the same save the user is watching.
_SYNC_MAX_WAITS = 30


def syncVersionIntoTDN(attempt=0):
    """Re-export the .tdn rows that carry par.Version, AFTER the bump.

    THE LAG THIS REMOVES. A single project.save fires onProjectPreSave on
    two different Execute DATs. The Embody COMP's own execute DAT runs
    first and exports every dirty TDN row -- reading par.Version as it
    stands, V. Only afterwards does THIS DAT bump the par to V+1 and bake
    release/Embody-v{V+1}.tox. So every release shipped a .tdn stamped one
    version behind its own .tox, provably: Embody.tdn's header carried
    `generator: Embody/6.0.222` beside `source_file: Embody-6.223.toe`.

    Nothing shipped wrong -- the .tox is built from the LIVE comp -- but
    the committed .tdn never matched the release it shipped with, and a
    COMP reconstructed from it came up a version behind. It also left both
    Embody rows permanently dirty, because the fingerprint of the live
    parameters no longer matched the file just written.

    Re-exporting here rather than after the bump inside pre-save is
    deliberate: that body has no fail-safe boundary, and an exception in
    it truncates the .toe to zero bytes (issue #21). This runs after the
    save is already on disk, so the worst case is a stale .tdn -- exactly
    what we have today.
    """
    try:
        embody = op.Embody
        if embody is None:
            return
        # The strip/restore cycle may still be putting COMPs back; a row
        # exported mid-restore would capture a hollow network.
        if embody.fetch('_tdn_stripped_paths', [], search=False):
            # BOUNDED. A flag left set by an interrupted restore would other-
            # wise re-run this every 5 frames for the rest of the session, and
            # silently: the sync that never happens is exactly the lag this
            # exists to stop, so give up loudly instead.
            if attempt >= _SYNC_MAX_WAITS:
                op.Embody.Warn(
                    'Version sync into .tdn gave up after %d waits: the TDN '
                    'restore flag is still set, so the .tdn files may lag the '
                    '.toe by one version. A manual save will re-run it.'
                    % _SYNC_MAX_WAITS)
                return
            run('me.module.syncVersionIntoTDN(attempt=%d)' % (attempt + 1),
                fromOP=me, delayFrames=5)
            return
        version = str(embody.par.Version.eval() or '')
        if not version:
            return
        table = embody.ext.Embody.Externalizations
        if table is None:
            return
        headers = [table[0, c].val for c in range(table.numCols)]
        try:
            path_col = headers.index('path')
            strategy_col = headers.index('strategy')
        except ValueError:
            return
        marker = 'generator: Embody/%s' % version
        for r in range(1, table.numRows):
            row_path = str(table[r, path_col].val or '')
            if str(table[r, strategy_col].val or '') != 'tdn':
                continue
            # Only the rows that CONTAIN the Embody COMP carry its Version.
            # '/' is excluded deliberately: re-exporting the whole project
            # root here would be a far larger write than this warrants.
            if not row_path or row_path == '/':
                continue
            if not (row_path == embody.path
                    or embody.path.startswith(row_path + '/')):
                continue
            try:
                rel = str(table[r, headers.index('rel_file_path')].val or '')
                abs_path = (embody.ext.Embody.buildAbsolutePath(rel)
                            if rel else None)
                if abs_path and abs_path.is_file():
                    with open(str(abs_path), 'r', encoding='utf-8') as handle:
                        head = [handle.readline() for _ in range(12)]
                    # LINE-EXACT, not `marker in head`: a substring test makes
                    # every version that is a strict PREFIX of the stamped one
                    # look current (6.0.22 against a file stamped 6.0.228), so
                    # the skip fires on the one release where the lag matters.
                    if any(line.strip() == marker for line in head):
                        continue        # already current -- idempotent
            except Exception:
                pass                    # unreadable: re-export rather than skip
            # bump_build=False: the release manifest already recorded
            # par.Build, so a second bump would put the manifest one
            # behind the .tdn -- the same drift, one size smaller.
            embody.ext.Embody.SaveTDN(row_path, bump_build=False)
    except Exception as e:
        debug(f'[execute_src_ctrl] version sync into .tdn failed: {e!r}')


def onProjectPostSave():
    # Deferred, and never allowed to raise: this runs after the .toe is
    # already on disk, and Embody's own post-save restore loop may still
    # be running whichever Execute DAT fired first.
    try:
        run('me.module.syncVersionIntoTDN()', fromOP=me, delayFrames=5)
    except Exception as e:
        debug(f'[execute_src_ctrl] could not schedule the version sync: {e!r}')
