"""
UpdaterExt -- Embody self-update from GitHub releases.

Hosted on the 'updater' baseCOMP inside the Embody COMP (Embody's four root
extension slots are occupied). Reaches the host via a CONTEXT-FREE reference
(self.ownerComp.parent.Embody) -- NOT the bare `parent.Embody` global, which
resolves from the current execution context. This matters because the
post-reload entry points (VerifyUpdate / VerifyRollback / _rollback) are
invoked by string-form run() whose scheduling op was destroyed, so they run
with ROOT as context where `parent` finds no Embody ancestor and raises. Every
host access here is bound to self.ownerComp (a stored reference), matching the
house precedent (WindowHeaderExt uses self.ownerComp.parent.Embody).

Swap mechanism (probe-verified 2026-07-21 on TD 2025.33070):
    The update is applied with an IN-PLACE external-tox reload -- set
    par.externaltox to the downloaded release .tox, reloadcustom/reloadbuiltin
    OFF, then pulse enableexternaltoxpulse. The host COMP survives (global
    shortcut, wires, position), children are rebuilt from the new tox, live
    custom-par VALUES are preserved while NEW par definitions land, the old
    extension instances get onDestroyTD (Envoy shuts down cleanly), and the
    reloaded execute DAT's onCreate runs Embody's normal boot chain.

    Consequences honored here:
    - Preserved par values mean par.Version still reads the OLD version after
      the reload; VerifyUpdate() stamps the About pars from the manifest --
      but ONLY after confirming a real reload happened (the EmbodyExt DAT's
      op id changes when children are recreated; a no-op reload keeps the old
      id, and stamping then would make par.Version lie).
    - externaltox is left pointing at the download; VerifyUpdate() clears it
      (same hazard _validateTrackedOperators clears for drag-ins).
    - The pulse destroys THIS extension's own host child mid-call, so the
      pulse is the LAST TD-touching statement of the apply path; the undo
      block is opened AND closed before the pulse, and everything after the
      reload runs from string-form run() callbacks that resolve the fresh
      instance via op('<embody>').op('updater') and are guarded against a
      missing child (a run() with delayRef=<destroyed op> never fires, so
      delayRef is never used here).

Failure surfacing:
    - CHECK-stage failures (no network, unverifiable/absent manifest, TD-build
      floor) are quiet on the automatic startup path (log + Update Status) and
      loud on the manual path (dialog). Nobody wants a dialog every launch
      because GitHub was briefly unreachable.
    - INSTALL-stage failures (backup export, reload, verify, rollback) ALWAYS
      dialog, on every path -- once the live component is being touched, a
      silent failure would leave the user unknowingly on a half-broken or
      wrong-version install. Silent success, loud failure.

Network layer follows the house pattern (EmbodyExt._checkMCPUpdate /
EnvoyExt._beginAsyncBootstrap): a daemon worker thread doing pure-Python
urllib with ZERO TD access publishes a generation-tagged result to a plain
attribute; a bounded main-thread run() poll chain (with a stale-instance
guard) drains it. urllib follows GitHub's 302 asset redirects and carries our
User-Agent (GitHub rejects UA-less requests with 403).

TD-2025 facts baked in: there is no project.dirty (the manual path offers a
save; the startup path runs when the just-opened .toe IS the recovery point),
and an older TD build loading a newer-build tox returns None/empty SILENTLY --
hence the manifest min_td_build gate BEFORE download and the reload-token
check AFTER.
"""

import hashlib
import json
import os
import re
from pathlib import Path


class UpdaterExt:
    """Self-updater for the Embody component (check / download / apply)."""

    GITHUB_OWNER = 'dylanroscover'
    GITHUB_REPO = 'Embody'
    USER_AGENT = 'Embody-Updater'
    MANIFEST_ASSET = 'embody-release.json'
    # A release tox is ~700KB; cap well above that but below anything that
    # could exhaust memory when buffered (GitHub allows 2GB assets).
    MAX_ASSET_BYTES = 50_000_000
    # A backup must look like a plausible portable tox, not a truncated stub.
    MIN_BACKUP_BYTES = 100_000
    ASSET_RE = re.compile(r'^[A-Za-z0-9._-]+\.tox$')

    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        # Worker handoff slots (plain attributes -- never TD objects).
        # None = in flight; dict (with '_gen') = published result.
        self._check_result = None
        self._download_result = None
        self._check_gen = 0
        self._download_gen = 0
        self._busy = False
        self._pending = None  # release info between check -> apply

    # ==================================================================
    # Host access (CONTEXT-FREE) + logging / dialogs (main thread only)
    # ==================================================================

    @property
    def _embody(self):
        # Bound to self.ownerComp, so it resolves correctly even from the
        # root execution context of a surviving string-form run().
        return self.ownerComp.parent.Embody

    def _log(self, msg, level='INFO'):
        try:
            self._embody.Log(f'Updater: {msg}', level)
        except Exception:
            debug(f'[Updater/{level}] {msg}')

    def _dialog(self, title, message, buttons):
        """Route through Embody's auto-response-aware messageBox.

        Returns the button index, or -1 when the dialog is suppressed (test
        runner active / save window / unseeded). Callers MUST treat -1 (and
        any non-affirmative value) as 'no' -- never as a default action.
        """
        return self._embody.ext.Embody._messageBox(title, message, buttons)

    @staticmethod
    def _posix(path):
        return str(path).replace('\\', '/')

    def _setPar(self, par, value):
        """Assign through the readOnly dance (About/status pars are locked)."""
        was = par.readOnly
        par.readOnly = False
        par.val = value
        par.readOnly = was

    def _status(self, text):
        p = getattr(self._embody.par, 'Updatestatus', None)
        if p is not None:
            self._setPar(p, str(text)[:160])

    # ==================================================================
    # Pure helpers (static -- unit-testable outside TD)
    # ==================================================================

    @staticmethod
    def parseVersion(tag):
        """'v6.0.141' / '6.0.141' -> (6, 0, 141); None if not X.Y.Z."""
        m = re.match(r'^v?(\d+)\.(\d+)\.(\d+)$', str(tag).strip())
        return tuple(int(g) for g in m.groups()) if m else None

    @staticmethod
    def parseBuild(build):
        """'2025.33070' -> (2025, 33070); None if malformed."""
        m = re.match(r'^(\d+)\.(\d+)$', str(build).strip())
        return tuple(int(g) for g in m.groups()) if m else None

    @staticmethod
    def validateManifest(data):
        """Return error string for a bad manifest dict, or None if usable.

        This is a security gate as much as a schema check: `asset` flows into
        a filesystem path and must be a bare .tox filename (no traversal, no
        absolute path); `size` must be sane before we buffer a download.
        """
        if not isinstance(data, dict):
            return 'manifest is not a JSON object'
        for key in ('version', 'asset', 'size', 'sha256', 'min_td_build'):
            if key not in data:
                return f'manifest missing required key: {key}'
        if UpdaterExt.parseVersion(data['version']) is None:
            return f'manifest version not X.Y.Z: {data["version"]!r}'
        if UpdaterExt.parseBuild(data['min_td_build']) is None:
            return f'manifest min_td_build malformed: {data["min_td_build"]!r}'
        if not isinstance(data['size'], int) or data['size'] <= 0:
            return 'manifest size must be a positive integer'
        if data['size'] > UpdaterExt.MAX_ASSET_BYTES:
            return (f'manifest size {data["size"]} exceeds the '
                    f'{UpdaterExt.MAX_ASSET_BYTES}-byte cap')
        if not re.match(r'^[0-9a-f]{64}$', str(data['sha256'])):
            return 'manifest sha256 is not a 64-char lowercase hex digest'
        if not UpdaterExt.ASSET_RE.match(str(data['asset'])):
            return (f'manifest asset must be a bare .tox filename, got '
                    f'{data["asset"]!r}')
        return None

    @staticmethod
    def apiLatestUrl(owner, repo):
        return f'https://api.github.com/repos/{owner}/{repo}/releases/latest'

    # ==================================================================
    # Paths and state files
    # ==================================================================

    def _updatesDir(self, create=False):
        root = self._embody.ext.Embody._findProjectRoot()
        d = Path(root) / '.embody' / 'updates'
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def _sentinelPath(self):
        return self._updatesDir(create=False) / 'pending.json'

    def _withinUpdates(self, path):
        """True only if `path` really lives inside .embody/updates/.

        pending.json is attacker-writable local state; the recovery path must
        never reload a backup pointed anywhere else on disk.
        """
        try:
            base = os.path.realpath(str(self._updatesDir(create=False)))
            target = os.path.realpath(str(path))
            return target == base or target.startswith(base + os.sep)
        except Exception:
            return False

    def _writeSentinel(self, data):
        self._updatesDir(create=True)
        path = self._sentinelPath()
        tmp = Path(str(path) + '.tmp')
        tmp.write_text(json.dumps(data, indent=1), encoding='utf-8')
        os.replace(str(tmp), str(path))

    def _readSentinel(self):
        path = self._sentinelPath()
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return None

    def _clearSentinel(self):
        try:
            self._sentinelPath().unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _sha256File(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest()

    # ==================================================================
    # Guards / status helpers
    # ==================================================================

    def isDevCheckout(self):
        """True when Embody's own DATs are file-synced (the git dev tree).

        ExportPortableTox strips every relative file reference from release
        toxes, so a non-empty file par on EmbodyExt only exists in the dev
        checkout -- where the repo, not a downloaded tox, is source of truth.
        """
        dat = self._embody.op('EmbodyExt')
        return bool(dat is not None and dat.par.file.eval())

    def _refuse(self, why, interactive):
        """Pre-commit refusal (nothing on disk touched). Quiet unless asked."""
        self._log(f'update refused: {why}', 'WARNING')
        self._status(why)
        if interactive:
            self._dialog('Embody Update', why, ['OK'])
        return {'error': why}

    def _fail(self, why, loud=True):
        """Install-stage failure -- the live component was being touched.

        ALWAYS dialogs when loud (the default): a silent failure here leaves
        the user unknowingly on a broken/wrong install.
        """
        self._log(f'update FAILED: {why}', 'ERROR')
        self._status(why)
        if loud:
            self._dialog('Embody Update', why, ['OK'])
        return {'error': why}

    # ==================================================================
    # CHECK (promoted): worker thread + bounded main-thread poll
    # ==================================================================

    def CheckForUpdate(self, interactive=True, auto_install=False):
        """Query GitHub for a newer release. Prompts when interactive."""
        if self._readSentinel():
            return self._refuse(
                'An update is already in progress. Restart TouchDesigner if '
                'this persists.', interactive)
        if self._busy:
            return self._refuse('An update operation is already running.',
                                interactive)
        if self.isDevCheckout():
            return self._refuse(
                'This is the Embody dev checkout -- update via git, '
                'not self-update.', interactive)
        local = self.parseVersion(self._embody.par.Version.eval())
        if local is None:
            return self._refuse('Local Version parameter is not X.Y.Z.',
                                interactive)

        self._busy = True
        self._check_result = None
        self._check_gen += 1
        gen = self._check_gen
        self._status('Checking for updates...')
        self._log(f'checking {self.GITHUB_OWNER}/{self.GITHUB_REPO} '
                  f'(local v{".".join(map(str, local))})')

        # Resolve EVERYTHING the worker needs on the main thread first.
        url = self.apiLatestUrl(self.GITHUB_OWNER, self.GITHUB_REPO)
        ua = self.USER_AGENT
        manifest_name = self.MANIFEST_ASSET

        def _worker():
            # ZERO TD access in here -- pure Python only.
            out = {'_gen': gen}
            try:
                import urllib.request
                req = urllib.request.Request(url, headers={
                    'User-Agent': ua,
                    'Accept': 'application/vnd.github+json',
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    release = json.loads(resp.read())
                out['tag'] = release.get('tag_name', '')
                out['notes'] = (release.get('body') or '')[:4000]
                assets = release.get('assets') or []
                out['assets'] = {
                    a.get('name'): {
                        'url': a.get('browser_download_url'),
                        'size': a.get('size'),
                    } for a in assets
                }
                mf = out['assets'].get(manifest_name)
                if mf and mf.get('url'):
                    req2 = urllib.request.Request(
                        mf['url'], headers={'User-Agent': ua})
                    with urllib.request.urlopen(req2, timeout=10) as resp2:
                        out['manifest'] = json.loads(resp2.read())
            except Exception as e:  # network errors are expected, not fatal
                out['error'] = f'{type(e).__name__}: {e}'
            self._check_result = out

        import threading
        threading.Thread(target=_worker, daemon=True).start()
        # Budget >= 3x the worker's worst case (two sequential 10s requests
        # plus unbounded DNS): 100 x 15 frames ~= 25s at 60fps.
        run('args[0]._pollCheck(args[1], args[2], args[3], args[4])',
            self, interactive, auto_install, gen, 0, delayFrames=15)
        return {'status': 'checking'}

    def _staleInstance(self):
        try:
            return self.ownerComp.ext.UpdaterExt is not self
        except Exception:
            return True

    def _pollCheck(self, interactive, auto_install, gen, attempts):
        if self._staleInstance():
            return
        result = self._check_result
        # Only accept the result from THIS check's worker generation.
        if result is None or result.get('_gen') != gen:
            if attempts < 100:
                run('args[0]._pollCheck(args[1], args[2], args[3], args[4])',
                    self, interactive, auto_install, gen, attempts + 1,
                    delayFrames=15)
            else:
                self._busy = False
                self._refuse('Update check timed out.', interactive)
            return
        self._check_result = None
        self._busy = False
        self._finishCheck(result, interactive, auto_install)

    def _finishCheck(self, result, interactive, auto_install):
        if 'error' in result:
            msg = (f'Update check failed (no internet or GitHub '
                   f'unreachable): {result["error"]}')
            self._log(msg, 'WARNING')
            self._status('Update check failed (network error)')
            if interactive:
                self._dialog('Embody Update', msg, ['OK'])
            return

        local = self.parseVersion(self._embody.par.Version.eval())
        remote = self.parseVersion(result.get('tag', ''))
        if remote is None:
            self._refuse(f'Release tag is not vX.Y.Z: '
                         f'{result.get("tag")!r}', interactive)
            return
        # releases/latest is commit-date ordered, NOT semver -- only a
        # strictly greater remote version is an update.
        if remote <= local:
            self._status(f'Up to date (v{".".join(map(str, local))})')
            self._log('up to date')
            if interactive:
                self._dialog('Embody Update',
                             'Embody is up to date '
                             f'(v{".".join(map(str, local))}).', ['OK'])
            return

        tag = result['tag']
        manifest = result.get('manifest')
        err = self.validateManifest(manifest) if manifest is not None else (
            'release has no embody-release.json manifest')
        if err:
            self._refuse(
                f'Update v{".".join(map(str, remote))} found, but it cannot '
                f'be verified: {err}. Update manually from GitHub.',
                interactive)
            return

        min_build = self.parseBuild(manifest['min_td_build'])
        this_build = self.parseBuild(app.build)
        if this_build is None or min_build is None or this_build < min_build:
            self._refuse(
                f'Update {tag} requires TouchDesigner build '
                f'{manifest["min_td_build"]}+ (this is {app.build}). '
                f'Update TouchDesigner first.', interactive)
            return

        asset = result['assets'].get(manifest['asset'])
        if not asset or not asset.get('url'):
            self._refuse(f'Release {tag} is missing its asset '
                         f'{manifest["asset"]!r}.', interactive)
            return

        self._pending = {
            'tag': tag,
            'version': manifest['version'],
            'asset_url': asset['url'],
            'manifest': manifest,
            'notes': result.get('notes', ''),
        }
        self._status(f'{tag} available')
        self._log(f'update available: {tag}')

        if auto_install:
            self._startDownload(interactive=False, apply_after=True)
            return
        if interactive:
            # Keep this a DECISION, not a reading assignment: version pair +
            # a link. Release-notes bodies (project intro, changelog bullets)
            # overwhelmed the dialog; anyone who wants them has the URL.
            choice = self._dialog(
                'Embody Update',
                f'Update available: {tag} (installed: '
                f'v{".".join(map(str, local))}).\n\n'
                f'Release notes: https://github.com/{self.GITHUB_OWNER}/'
                f'{self.GITHUB_REPO}/releases/tag/{tag}\n\n'
                'Download and install now?',
                ['Install', 'Not Now'])
            if choice == 0:  # affirmative only; -1/1/None => do nothing
                self._startDownload(interactive=True, apply_after=True)

    # ==================================================================
    # DOWNLOAD: worker thread writes + hashes the asset, poll drains
    # ==================================================================

    def _startDownload(self, interactive, apply_after):
        if self._busy:
            return self._refuse('An update operation is already running.',
                                interactive)
        pending = self._pending
        if not pending:
            return self._refuse('No pending update to download.', interactive)

        self._busy = True
        self._download_result = None
        self._download_gen += 1
        gen = self._download_gen
        self._status(f'Downloading {pending["tag"]}...')

        url = pending['asset_url']
        ua = self.USER_AGENT
        expect_size = pending['manifest']['size']
        expect_sha = pending['manifest']['sha256']
        # asset is validated as a bare .tox filename -> safe to join.
        dest = self._posix(self._updatesDir(create=True)
                           / pending['manifest']['asset'])

        def _worker():
            # ZERO TD access. urllib follows the 302 to
            # objects.githubusercontent.com natively.
            out = {'_gen': gen}
            try:
                import urllib.request
                req = urllib.request.Request(
                    url, headers={'User-Agent': ua})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    # Cap the read at the manifest size (+1 to detect
                    # overrun) so a hostile server can't stream unbounded
                    # bytes into memory.
                    payload = resp.read(expect_size + 1)
                if len(payload) != expect_size:
                    raise ValueError(
                        f'size mismatch: got {len(payload)}, '
                        f'manifest says {expect_size}')
                digest = hashlib.sha256(payload).hexdigest()
                if digest != expect_sha:
                    raise ValueError('sha256 mismatch: download does not '
                                     'match the release manifest')
                tmp = dest + '.tmp'
                with open(tmp, 'wb') as f:
                    f.write(payload)
                os.replace(tmp, dest)
                out['path'] = dest
            except Exception as e:
                out['error'] = f'{type(e).__name__}: {e}'
            self._download_result = out

        import threading
        threading.Thread(target=_worker, daemon=True).start()
        run('args[0]._pollDownload(args[1], args[2], args[3], args[4])',
            self, interactive, apply_after, gen, 0, delayFrames=15)
        return {'status': 'downloading'}

    def _pollDownload(self, interactive, apply_after, gen, attempts):
        if self._staleInstance():
            return
        result = self._download_result
        if result is None or result.get('_gen') != gen:
            if attempts < 800:  # ~200s at 60fps; covers a slow trickle
                run('args[0]._pollDownload(args[1], args[2], args[3], args[4])',
                    self, interactive, apply_after, gen, attempts + 1,
                    delayFrames=15)
            else:
                self._busy = False
                self._refuse('Download timed out.', interactive)
            return
        self._download_result = None
        self._busy = False
        if 'error' in result:
            self._refuse(f'Download failed: {result["error"]}', interactive)
            return
        self._pending['tox_path'] = result['path']
        self._log(f'downloaded and verified {result["path"]}')
        if apply_after:
            self.ApplyUpdate(interactive=interactive)

    # ==================================================================
    # APPLY (promoted): pre-flight, backup, then the in-place reload
    # ==================================================================

    def ApplyUpdate(self, interactive=True):
        pending = self._pending
        if not pending or not pending.get('tox_path'):
            return self._refuse('No verified download to apply. Run '
                                'CheckForUpdate first.', interactive)
        if self._readSentinel():
            return self._refuse('An update is already in progress.',
                                interactive)
        if self._busy:
            return self._refuse('An update operation is already running.',
                                interactive)
        if self.isDevCheckout():
            return self._refuse('Dev checkout -- refusing self-update.',
                                interactive)
        if not os.path.isfile(pending['tox_path']):
            return self._refuse('Downloaded file vanished; re-run the check.',
                                interactive)
        # Re-gate the TD-build floor (cheap; app.build is constant in-session,
        # but this keeps apply self-contained and honest).
        min_build = self.parseBuild(pending['manifest']['min_td_build'])
        this_build = self.parseBuild(app.build)
        if this_build is None or min_build is None or this_build < min_build:
            return self._refuse(
                f'Update {pending["tag"]} requires TouchDesigner build '
                f'{pending["manifest"]["min_td_build"]}+.', interactive)

        # There is no project.dirty on TD 2025 -- offer the recovery point
        # explicitly. Whitelist affirmatives: -1 (suppressed) / None / any
        # unexpected value must CANCEL, never install.
        if interactive:
            choice = self._dialog(
                'Embody Update',
                f'Install {pending["tag"]} now?\n\n'
                'Saving the project first gives you a clean recovery '
                'point in case anything goes wrong.',
                ['Save and Install', 'Install Without Saving', 'Cancel'])
            if choice not in (0, 1):
                self._status(f'{pending["tag"]} available')
                return {'status': 'cancelled'}
            if choice == 0:
                project.save()

        self._busy = True
        self._status(f'Installing {pending["tag"]}...')
        # Delay past the post-save dialog-suppression window (~120 frames)
        # AND the Envoy socket-release window, so backup-failure dialogs in
        # phase 2 are not swallowed.
        run('args[0]._applyPhase2(args[1])', self, interactive,
            delayFrames=150)
        return {'status': 'applying'}

    def _applyPhase2(self, interactive):
        if self._staleInstance():
            return
        pending = self._pending
        embody = self._embody

        # Stop Envoy cleanly so the port is free and its registry entry is
        # removed (onDestroyTD alone does not remove the envoy.json entry).
        # Gate on Envoyenable, not the status string (which reads
        # 'Running on port N' / 'Restarting after save...').
        try:
            if embody.par.Envoyenable.eval():
                embody.ext.Envoy.Stop()
                self._log('Envoy stopped for update')
        except Exception as e:
            self._log(f'Envoy stop skipped: {e!r}', 'WARNING')

        # Rollback artifact FIRST, verified before anything is touched.
        old_version = embody.par.Version.eval()
        backup = self._posix(self._updatesDir(create=True)
                            / f'backup-v{old_version}.tox')
        try:
            # run_hooks=False: this backup is update machinery, not an
            # authored release. Embody-self exports always run in LIVE
            # hook mode (never copy-staged), so hook DATs authored in the
            # DEV project ship inside the released Embody tox -- and
            # would execute here, inside an end user's project, or abort
            # the backup. Suppress them.
            ok = embody.ext.Embody.ExportPortableTox(target=embody,
                                                     save_path=backup,
                                                     run_hooks=False)
        except Exception as e:
            self._busy = False
            self._fail(f'Backup export failed -- update aborted: {e!r}')
            return
        if not ok:
            # ExportPortableTox reports failures via its return value
            # (export errors are exception-contained) -- without this
            # gate a STALE backup from a prior attempt could pass the
            # isfile/size checks below and become the rollback artifact.
            self._busy = False
            self._fail('Backup export reported failure -- update aborted.')
            return
        if (not os.path.isfile(backup)
                or os.path.getsize(backup) < self.MIN_BACKUP_BYTES):
            self._busy = False
            self._fail('Backup tox missing or implausibly small -- '
                       'update aborted.')
            return
        backup_sha = self._sha256File(backup)

        # Reload token: the EmbodyExt DAT's op id changes when children are
        # recreated by a REAL reload. A no-op/failed reload keeps the old id,
        # and VerifyUpdate refuses to stamp success in that case (so
        # par.Version can never lie about an install that didn't happen).
        try:
            reload_token = embody.op('EmbodyExt').id
        except Exception:
            reload_token = None

        # Crash sentinel: if TD dies mid-swap, the next open finds this and
        # offers the (integrity-checked) backup.
        self._writeSentinel({
            'from_version': old_version,
            'to_version': pending['version'],
            'tag': pending['tag'],
            'tox_path': pending['tox_path'],
            'backup_path': backup,
            'backup_sha256': backup_sha,
            'reload_token': reload_token,
            'manifest': pending['manifest'],
            'interactive': bool(interactive),
            'phase': 'reloading',
        })
        self._log(f'applying {pending["tag"]}: in-place reload from '
                  f'{pending["tox_path"]} (backup: {backup})')

        # Post-reload verifier: STRING form, resolves the FRESH instance via
        # the surviving host COMP, guarded against a missing 'updater' child,
        # no delayRef, generous delay for the new version's boot chain. This
        # run survives destruction of this extension's own host child.
        ep = embody.path
        verify = (f"op('{ep}').op('updater').ext.UpdaterExt.VerifyUpdate(0) "
                  f"if op('{ep}') and op('{ep}').op('updater') else None")
        run(verify, delayFrames=300)

        # ---- The reload. The undo block is opened AND closed here, so the
        # pulse is the LAST TD-touching statement (its own host dies with it).
        p = embody.par.externaltox
        ui.undo.startBlock('Embody self-update', enable=False)
        was = p.readOnly
        p.readOnly = False
        p.val = pending['tox_path']
        p.readOnly = was
        embody.par.reloadcustom = False
        embody.par.reloadbuiltin = False
        embody.par.enableexternaltox = True
        ui.undo.endBlock()
        embody.par.enableexternaltoxpulse.pulse()

    # ==================================================================
    # VERIFY (promoted): runs in the NEW extension instance post-reload
    # ==================================================================

    def VerifyUpdate(self, attempts=0):
        """Confirm the reload booted; stamp About pars; clean up or roll back."""
        sentinel = self._readSentinel()
        if not sentinel:
            self._log('VerifyUpdate: no pending sentinel -- nothing to do',
                      'WARNING')
            return
        embody = self._embody
        ext_dat = embody.op('EmbodyExt')
        # A REAL reload recreated the children, so EmbodyExt has a new op id.
        reloaded = (ext_dat is not None
                    and ext_dat.id != sentinel.get('reload_token'))
        booted = (reloaded
                  and embody.op('execute') is not None
                  and embody.extensionsReady)
        if not booted:
            if attempts < 10:
                ep = embody.path
                run(f"op('{ep}').op('updater').ext.UpdaterExt"
                    f".VerifyUpdate({attempts + 1}) "
                    f"if op('{ep}') and op('{ep}').op('updater') else None",
                    delayFrames=120)
                return
            self._rollback(sentinel, 'new version never finished booting')
            return

        manifest = sentinel['manifest']
        self._stampAboutPars(manifest)
        self._clearExternalTox()
        self._cleanupFiles(sentinel, keep_backup=True)
        self._clearSentinel()
        self._status(f'Updated to {sentinel["tag"]}')
        self._log(f'update to {sentinel["tag"]} verified '
                  f'(from v{sentinel["from_version"]})', 'SUCCESS')
        if sentinel.get('interactive'):
            self._dialog('Embody Update',
                         f'Embody was updated to {sentinel["tag"]}.\n\n'
                         'Settings and externalizations were preserved.',
                         ['OK'])

    def _stampAboutPars(self, manifest):
        """Preserved par values keep the OLD About info -- stamp the new."""
        embody = self._embody
        stamps = {
            'Version': manifest.get('version'),
            'Touchbuild': manifest.get('td_build'),
            'Build': manifest.get('build'),
            'Date': manifest.get('date'),
        }
        for name, value in stamps.items():
            if value is None:
                continue
            par = getattr(embody.par, name, None)
            if par is not None:
                self._setPar(par, value)

    def _clearExternalTox(self):
        """Detach from the downloaded file so a later save can't clobber it."""
        embody = self._embody
        p = embody.par.externaltox
        was = p.readOnly
        p.readOnly = False
        p.val = ''
        p.readOnly = was
        embody.par.enableexternaltox = False

    def _cleanupFiles(self, sentinel, keep_backup=True):
        """Remove the applied download; keep only the most recent backup."""
        try:
            tox = sentinel.get('tox_path')
            if tox and os.path.isfile(tox) and self._withinUpdates(tox):
                os.unlink(tox)
        except OSError:
            pass
        if keep_backup:
            return
        try:
            b = sentinel.get('backup_path')
            if b and os.path.isfile(b) and self._withinUpdates(b):
                os.unlink(b)
        except OSError:
            pass

    # ==================================================================
    # ROLLBACK
    # ==================================================================

    def _validBackup(self, sentinel):
        """The backup must be inside updates/ and match its recorded hash."""
        backup = sentinel.get('backup_path')
        if not backup or not os.path.isfile(backup):
            return None, 'backup file is missing'
        if not self._withinUpdates(backup):
            return None, 'backup path is outside .embody/updates'
        want = sentinel.get('backup_sha256')
        if want and self._sha256File(backup) != want:
            return None, 'backup failed its integrity check'
        return backup, None

    def _rollback(self, sentinel, why):
        self._log(f'update FAILED ({why}) -- rolling back to '
                  f'v{sentinel.get("from_version")}', 'ERROR')
        backup, berr = self._validBackup(sentinel)
        if backup is None:
            self._status('Update FAILED; backup unusable -- reopen saved .toe')
            sentinel['phase'] = 'rollback_failed'
            self._writeSentinel(sentinel)
            self._dialog(
                'Embody Update FAILED',
                f'The update failed ({why}) and the backup is unusable '
                f'({berr}). Close WITHOUT saving and reopen the project to '
                'recover.', ['OK'])
            return
        sentinel['phase'] = 'rolling_back'
        self._writeSentinel(sentinel)
        embody = self._embody
        ep = embody.path
        try:
            reload_token = embody.op('EmbodyExt').id
        except Exception:
            reload_token = None
        sentinel['rollback_token'] = reload_token
        self._writeSentinel(sentinel)
        run(f"op('{ep}').op('updater').ext.UpdaterExt.VerifyRollback(0) "
            f"if op('{ep}') and op('{ep}').op('updater') else None",
            delayFrames=300)
        p = embody.par.externaltox
        ui.undo.startBlock('Embody update rollback', enable=False)
        was = p.readOnly
        p.readOnly = False
        p.val = backup
        p.readOnly = was
        embody.par.reloadcustom = False
        embody.par.reloadbuiltin = False
        embody.par.enableexternaltox = True
        ui.undo.endBlock()
        embody.par.enableexternaltoxpulse.pulse()

    def VerifyRollback(self, attempts=0):
        sentinel = self._readSentinel()
        embody = self._embody
        ext_dat = embody.op('EmbodyExt')
        reloaded = (ext_dat is not None and sentinel is not None
                    and ext_dat.id != sentinel.get('rollback_token'))
        booted = reloaded and embody.extensionsReady
        if not booted and attempts < 10:
            ep = embody.path
            run(f"op('{ep}').op('updater').ext.UpdaterExt"
                f".VerifyRollback({attempts + 1}) "
                f"if op('{ep}') and op('{ep}').op('updater') else None",
                delayFrames=120)
            return
        self._clearExternalTox()
        if booted:
            self._clearSentinel()
            self._status('Update failed -- previous version restored')
            self._dialog(
                'Embody Update',
                'The update failed and the previous version was restored '
                'from backup. Details are in the Embody log.', ['OK'])
        else:
            # Rollback itself failed -- KEEP the sentinel so the next open
            # can re-offer recovery. This is exactly when it matters most.
            if sentinel is not None:
                sentinel['phase'] = 'rollback_failed'
                self._writeSentinel(sentinel)
            self._status('Update AND rollback failed -- reopen saved .toe')
            self._dialog(
                'Embody Update FAILED',
                'The update and the automatic rollback both failed. Close '
                'WITHOUT saving and reopen the project to recover the last '
                'saved state.', ['OK'])

    # ==================================================================
    # STARTUP (promoted): called from execute.py at ~frame 150
    # ==================================================================

    def StartupCheck(self):
        """Crash-recovery sweep, then the Autoupdate-gated auto check."""
        sentinel = self._readSentinel()
        if sentinel:
            # A previous update never completed (TD crashed or was closed
            # mid-swap). Surface it regardless of the Autoupdate setting.
            backup, berr = self._validBackup(sentinel)
            if backup is None:
                self._log(f'interrupted update found but backup unusable '
                          f'({berr}); clearing sentinel', 'WARNING')
                self._dialog(
                    'Embody Update',
                    f'A previous update to {sentinel.get("tag")} did not '
                    f'complete and its backup is unusable ({berr}). No '
                    'automatic recovery is possible.', ['OK'])
                self._clearSentinel()
                return
            choice = self._dialog(
                'Embody Update Recovery',
                f'An update to {sentinel.get("tag")} did not complete. '
                'Restore the pre-update backup?',
                ['Restore Backup', 'Keep Current State'])
            if choice == 0:
                self._rollback(sentinel, 'recovering interrupted update')
            elif choice == 1:
                # Explicit "keep" only -- a suppressed/dismissed dialog (-1)
                # leaves the sentinel so the next open re-offers recovery.
                self._clearSentinel()
            return

        mode = 'off'
        p = getattr(self._embody.par, 'Autoupdate', None)
        if p is not None:
            mode = str(p.eval())
        if mode == 'off':
            # Truthful resting state, never a blank: an empty read-only
            # status field looks broken on a fresh install, and 'Disabled'
            # also replaces a stale result left by a session that had
            # checks enabled.
            self._status('Disabled')
            return
        if self.isDevCheckout():
            return  # silent -- dev tree updates via git
        # notify: check + status/log only. install: check + full apply.
        # CHECK-stage failures stay quiet on this auto path; INSTALL-stage
        # failures (backup/reload/verify/rollback) always dialog via _fail.
        self.CheckForUpdate(interactive=False,
                            auto_install=(mode == 'install'))
