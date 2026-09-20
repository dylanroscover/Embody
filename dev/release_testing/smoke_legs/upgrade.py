"""
Smoke leg `upgrade`: the installed PREVIOUS release self-updates to the build
under test, rolls back to the updater's own backup, then updates again.

HERMETIC -- nothing here reaches GitHub. UpdaterExt exposes no manifest/asset
override, so the leg seeds the updater's `_pending` slot (the dict a check
would have produced) from the repo's release/embody-release.json and a copy of
ctx['build']['tox'] staged in .embody/updates, where a real download lands.
Everything after that is the product's own path: ApplyUpdate -> backup export
-> in-place swap -> VerifyUpdate, then the leftover-sentinel recovery ->
_rollback -> VerifyRollback. The network CHECK stage is therefore NOT covered
here (a fresh install ships Autoupdate=notify, so the smoke's own startup
already runs it).

Three mechanisms the assertions are built on:
- Updatestatus is NOT a witness in either direction: the reloaded component
  runs its own notify-mode startup check within seconds and overwrites
  'Updated to vNEW' (and the rollback's line) with 'Up to date (vNEW)'
  (measured 2026-09-19). The swap also PRESERVES par values, so par.Version
  proves an update but never a rollback -- nothing stamps it back. Both
  directions are pinned instead on a fingerprint of the extension DAT sources
  (the component's identity) and on the sentinel file, which only
  VerifyUpdate / VerifyRollback delete, each after confirming the EmbodyExt
  op id changed.
- The swap REPLACES the COMP's contents, wiping the storage that holds
  _smoke_test_responses -- without it EmbodyExt._messageBox opens a REAL modal
  and freezes the smoke TD, so staggered run()s re-arm the seed across a swap.
- An Envoy call must never swap its own host synchronously (issue #110): the
  rollback trigger is deferred as one run() string. ApplyUpdate is safe called
  directly (its phase 2 is 150 frames out).

The final re-update is budget-gated: it is what leaves the project on the NEW
build for any leg that follows, so when it is skipped the step says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

_POLL_S = 2.0
_RELOAD_TIMEOUT_S = 180.0   # swap + boot chain + Envoy restart, generously
_RESERVE_S = 25.0           # left for the orchestrator's teardown
_PROBE_TIMEOUT_S = 10.0
_MIN_BACKUP_BYTES = 100_000  # UpdaterExt._MIN_BACKUP_BYTES
_SENTINEL_KEY = '__smoke_sentinel__'

# --- code run inside the smoke TD (execute_python returns str(result)) ------

_STATE = """
import hashlib, json, td
emb = op.Embody
u = emb.op('updater').ext.UpdaterExt
src = []
for n in ('EmbodyExt', 'EnvoyExt', 'TDXNExt'):
    d = emb.op(n)
    src.append(d.text if d is not None else '')
result = json.dumps({
    'path': emb.path,
    'folder': str(td.project.folder),
    'token': (emb.op('EmbodyExt').id if emb.op('EmbodyExt') is not None else 0),
    'version': str(emb.par.Version.eval()),
    'status': str(emb.par.Status.eval()),
    'envoy': str(emb.par.Envoystatus.eval()),
    'update_status': str(emb.par.Updatestatus.eval()),
    'autoupdate': str(emb.par.Autoupdate.eval()),
    'fingerprint': hashlib.sha256('\\n'.join(src).encode('utf-8')).hexdigest(),
    'updates_dir': str(u._updatesDir(True)),
    'busy': bool(getattr(u, '_busy', False)),
    'busy_phase': str(getattr(u, '_busy_phase', '')),
    'ready': bool(emb.extensionsReady),
})
"""

_SEED = """
import td
emb = op.Embody
seed = %r
emb.store('_smoke_test_responses', dict(seed))
for f in range(30, 1801, 120):
    td.run("op(args[1]).store('_smoke_test_responses', dict(args[0]))"
           " if op(args[1]) else None", seed, emb.path, delayFrames=f)
result = emb.path
"""

_APPLY = """
import json
u = op.Embody.op('updater').ext.UpdaterExt
with open(%r, encoding='utf-8') as f:
    mf = json.load(f)
u._pending = {'tag': mf.get('tag') or ('v' + str(mf['version'])),
              'version': str(mf['version']), 'asset_url': '',
              'manifest': mf, 'notes': '', 'tox_path': %r}
result = json.dumps(u.ApplyUpdate(interactive=False))
"""

_UPTODATE = """
u = op.Embody.op('updater').ext.UpdaterExt
u._finishCheck({'tag': %r, 'assets': {}, 'manifest': None}, False, False)
result = str(op.Embody.par.Updatestatus.eval())
"""

_ROLLBACK_ARM = """
import json, td
u = op.Embody.op('updater').ext.UpdaterExt
u._writeSentinel(json.loads(%r))
td.run("op(args[0]).op('updater').ext.UpdaterExt.CheckForUpdate(True)"
       " if op(args[0]) else None", op.Embody.path, delayFrames=30)
result = 'armed'
"""

# What VerifyRollback writes before the startup check overwrites it --
# reported as evidence, never waited on.
_ROLLED_BACK = 'Update failed -- previous version restored'


def _sha256(path):
    with open(path, 'rb') as f:  # release toxes are ~2MB
        return hashlib.sha256(f.read()).hexdigest()


def _brief(state):
    """The keys a failed wait is diagnosed from -- the whole state dict
    buries them."""
    if not state:
        return state
    out = {k: state.get(k) for k in ('version', 'status', 'update_status',
                                     'envoy', 'ready', 'busy', 'busy_phase')}
    out['fingerprint'] = str(state.get('fingerprint'))[:12] + '...'
    return out


def _inside(path, root):
    try:
        root = os.path.realpath(root)
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except ValueError:  # different drives on Windows
        return False


def registry_port(run_dir, pid=None):
    """The smoke Envoy's port from .embody/envoy.json -- the only witness
    while MCP is down. Prefers the entry registered to `pid`."""
    try:
        with open(os.path.join(run_dir, '.embody', 'envoy.json'),
                  encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    instances = data.get('instances')
    if not isinstance(instances, dict):
        return None
    entries = [e for e in instances.values() if isinstance(e, dict)]
    mine = [e for e in entries if pid and str(e.get('td_pid')) == str(pid)]
    active = [e for e in [instances.get(data.get('active'))]
              if isinstance(e, dict)]
    for entry in mine + active + entries:
        if entry.get('port'):
            return int(entry['port'])
    return None


class _Abort(Exception):
    """A step failed; the leg stops -- a half-updated project proves nothing."""


def run(ctx, clock=None, sleep=None):
    """smoke_legs contract entry point. `clock`/`sleep` are test seams."""
    leg = _Upgrade(ctx, clock or time.monotonic, sleep or time.sleep)
    try:
        leg.execute()
    except _Abort as e:
        leg.error = str(e)
    except Exception as e:  # never raise out of run()
        leg.error = f'{type(e).__name__}: {e}'
    ok = bool(leg.steps) and not leg.error and all(s['ok'] for s in leg.steps)
    return {'ok': ok, 'steps': leg.steps, 'error': leg.error}


class _Upgrade:

    def __init__(self, ctx, clock, sleep):
        self.ctx = ctx
        self.clock = clock
        self.sleep = sleep
        self.steps = []
        self.error = ''
        self.port = int(ctx.get('port') or 0)
        self.embody = '/Embody'
        self.old = str((ctx.get('installed') or {}).get('version') or '')
        self.new = str((ctx.get('build') or {}).get('version') or '')
        self.fp_old = ''
        self.errors_before = 0
        self.probe_error = ''
        self.run_dir = os.path.realpath(str(ctx['run_dir']))

    # ---- steps -----------------------------------------------------------

    def _ok(self, step, detail=''):
        self.steps.append({'step': step, 'ok': True, 'detail': detail})

    def _fail(self, step, detail):
        self.steps.append({'step': step, 'ok': False, 'detail': detail})
        raise _Abort(f'{step}: {detail}')

    def _check(self, step, ok, detail):
        (self._ok if ok else self._fail)(step, detail)

    # ---- talking to the smoke TD ----------------------------------------

    def _py(self, code, timeout=30):
        return self.ctx['py'](code, timeout)

    def _probe(self):
        """Live state, or None while Envoy/Embody is not answering -- every
        failure mode of a swap in flight looks the same here, so the reason is
        kept: a BROKEN probe must not read as 'Envoy never answered'.

        A port is only believed while it answers for THIS run directory. The
        swap frees a port, and a port freed on a developer machine can be
        re-bound by another TouchDesigner between two polls."""
        try:
            state = json.loads(self._py(_STATE, _PROBE_TIMEOUT_S))
        except Exception as e:
            self.probe_error = f'{type(e).__name__}: {e}'
            return None
        if os.path.realpath(str(state.get('folder'))) != self.run_dir:
            self.probe_error = (f"port {self.port} answers for "
                                f"{state.get('folder')!r}, not the run dir")
            return None
        return state

    def _reconnect(self):
        port = registry_port(self.ctx['run_dir'], self.ctx.get('pid'))
        if port and port != self.port:
            self.ctx['set_port'](port)
            self.ctx['log'](f'upgrade: Envoy came back on port {port}')
            self.port = port

    def _wait(self, step, want, detail, timeout=_RELOAD_TIMEOUT_S):
        """Poll the component until `want(state)`, re-reading the port each
        round. Fails the step (and the leg) on timeout or an empty budget."""
        window = min(timeout, max(0.0, self.ctx['budget']() - _RESERVE_S))
        if window <= 0:
            self._fail(step, f'{detail}: no budget left in the run')
        deadline = self.clock() + window
        last = None
        while self.clock() < deadline:
            state = self._probe()
            if state is None:
                self._reconnect()
            else:
                last = state
                if want(state):
                    return state
            self.sleep(_POLL_S)
        self._fail(step, f'{detail} within {window:.0f}s; last seen '
                         f'{_brief(last)}')

    def _tool(self, name, arguments, key):
        """One MCP read; None when the tool errors (itself a failure the
        caller reports)."""
        try:
            return self.ctx['call'](name, arguments, 30).get(key)
        except Exception:
            return None

    def _errors(self):
        return self._tool('get_op_errors',
                          {'op_path': self.embody, 'recurse': True},
                          'errorCount')

    def _arm_dialogs(self, extra=None):
        """Keep EmbodyExt._messageBox answering -1 instead of opening a modal
        that would freeze the smoke TD. The never-consumed sentinel key keeps
        the store alive; an EMPTY store falls through to a real dialog."""
        seed = {_SENTINEL_KEY: 0}
        seed.update(extra or {})
        path = self._py(_SEED % (seed,), 20)
        if path:
            self.embody = str(path)
        return path

    # ---- phases ----------------------------------------------------------

    def _manifest(self):
        """The release manifest describing ctx['build'] -- the leg refuses to
        install a .tox the manifest does not name."""
        build = self.ctx['build']
        repo = self.ctx.get('repo') or build.get('repo') or ''
        path = os.path.join(str(repo), 'release', 'embody-release.json')
        try:
            with open(path, encoding='utf-8') as f:
                manifest = json.load(f)
        except Exception as e:
            self._fail('manifest', f'{path}: {type(e).__name__}: {e}')
        bad = [k for k, want in (('version', build.get('version')),
                                 ('sha256', build.get('sha256')),
                                 ('size', build.get('size')))
               if want and str(manifest.get(k)) != str(want)]
        if bad:
            self._fail('manifest', f'{path} does not describe the build under '
                                   f'test -- mismatched: {", ".join(bad)}')
        self._ok('manifest', f"v{manifest.get('version')} "
                             f"asset={manifest.get('asset')} "
                             f"min_td_build={manifest.get('min_td_build')}")
        return manifest, path

    def _stage(self, updates_dir, manifest, step):
        """Copy the build under test where a download would have landed, so
        the updater's own cleanup owns the file and the repo asset is never
        touched (VerifyUpdate unlinks the tox it installed)."""
        dest = os.path.join(updates_dir, str(manifest['asset']))
        if not _inside(dest, self.ctx['run_dir']):
            self._fail(step, f'{dest} is outside the run directory')
        os.makedirs(updates_dir, exist_ok=True)
        shutil.copyfile(self.ctx['build']['tox'], dest)
        digest = _sha256(dest)
        self._check(step, digest == manifest['sha256'],
                    f'{dest} sha256={digest[:12]}... '
                    f'(manifest {str(manifest.get("sha256"))[:12]}...)')
        return dest.replace('\\', '/')

    def _update(self, manifest, manifest_path, updates_dir, tag, was):
        """Seed _pending and drive one real ApplyUpdate to a verified new
        version. `was` is the state before the apply: a new EmbodyExt op id
        (the updater's own reload token) says the swap happened, par.Version
        says VerifyUpdate accepted it, and the cleared sentinel says it
        finished."""
        staged = self._stage(updates_dir, manifest, f'stage_{tag}')
        # A component that just booted runs its own notify-mode check, and
        # ApplyUpdate refuses while that latch is up -- what a user clicking
        # Install mid-check gets. Measured 2026-09-19: the re-update fired
        # ~1s after the rollback and was refused.
        self._wait(f'apply_{tag}', lambda s: not s['busy'],
                   'the updater never left its busy phase', timeout=90)
        reply = json.loads(self._py(_APPLY % (manifest_path, staged), 30))
        self._check(f'apply_{tag}', reply.get('status') == 'applying',
                    f'ApplyUpdate(interactive=False) -> {reply}')
        t0 = self.clock()
        state = self._wait(f'verify_{tag}',
                           lambda s: (s['token'] != was['token']
                                      and s['version'] == self.new
                                      and s['ready']
                                      and self._sentinel_cleared(updates_dir)),
                           f'the component never became a verified v{self.new}')
        self._ok(f'verify_{tag}',
                 f"Version={state['version']} EmbodyExt id {was['token']} -> "
                 f"{state['token']} after {self.clock() - t0:.0f}s on port "
                 f"{self.port}; VerifyUpdate cleared the sentinel; "
                 f"Updatestatus={state['update_status']!r}")
        return state

    def _sentinel_cleared(self, updates_dir):
        """VerifyUpdate / VerifyRollback delete pending.json only after each
        confirms a real reload -- the one race-free witness on disk."""
        return not os.path.isfile(os.path.join(updates_dir, 'pending.json'))

    def _health(self, step, state):
        errors = self._errors()
        self._check(step, (state['status'] == 'Enabled'
                           and errors is not None
                           and errors <= self.errors_before
                           and state['envoy'] == f'Running on port {self.port}'),
                    f"Status={state['status']} errorCount={errors} "
                    f"(was {self.errors_before}) "
                    f"Envoystatus={state['envoy']!r}")

    def _rollback(self, updates_dir, was):
        """Drive the user-visible recovery: a leftover sentinel naming the
        backup, then CheckForUpdate -> 'Restore Backup' -> _rollback."""
        backup = os.path.join(updates_dir, f'backup-v{self.old}.tox')
        size = os.path.getsize(backup) if os.path.isfile(backup) else 0
        self._check('backup', size >= _MIN_BACKUP_BYTES,
                    f'{backup} {size} bytes (a fresh ExportPortableTox of the '
                    f'live v{self.old} COMP, so its sha256 is deliberately '
                    f'NOT the installed tox\'s)')
        sentinel = {'from_version': self.old, 'to_version': self.new,
                    'tag': f'v{self.new}', 'tox_path': '',
                    'backup_path': backup.replace('\\', '/'),
                    'backup_sha256': _sha256(backup), 'phase': 'reloading',
                    'session': {'pid': 0, 'started': 0}}
        # 'Embody Update' -> button 0 IS the Restore Backup answer; the
        # sentinel key keeps the store alive after it is consumed.
        self._arm_dialogs({'Embody Update': 0})
        self._check('rollback_trigger',
                    self._py(_ROLLBACK_ARM % (json.dumps(sentinel),), 30)
                    == 'armed',
                    f'leftover sentinel written; CheckForUpdate deferred '
                    f'(backup {os.path.basename(backup)})')
        rolled = self._wait('rollback',
                            lambda s: (s['token'] != was['token']
                                       and s['ready']
                                       and self._sentinel_cleared(updates_dir)),
                            'the backup was never reloaded')
        self._ok('rollback',
                 f"EmbodyExt id {was['token']} -> {rolled['token']}; "
                 f"VerifyRollback cleared the sentinel after writing "
                 f"{_ROLLED_BACK!r}; Updatestatus now "
                 f"{rolled['update_status']!r}; par.Version reads "
                 f"{rolled['version']} -- the swap preserves par values and "
                 f"nothing stamps them back")
        self._check('rollback_identity', rolled['fingerprint'] == self.fp_old,
                    f"{rolled['fingerprint'][:12]}... == the pre-update "
                    f"{self.fp_old[:12]}...")
        self._health('rollback_health', rolled)
        return rolled

    # ---- the leg ---------------------------------------------------------

    def execute(self):
        if not self.new or not self.old or self.old == self.new:
            self._fail('preconditions',
                       f'nothing to upgrade: installed v{self.old!r}, build '
                       f'v{self.new!r} -- --legs upgrade stages the previous '
                       f'release before boot')
        state = self._probe()
        if state is None:
            self._fail('installed_version',
                       f'Envoy did not answer on port {self.port}: '
                       f'{self.probe_error}')
        self.embody = state['path']
        before = state
        self.fp_old = state['fingerprint']
        self.errors_before = self._errors() or 0
        ext_before = self._tool('get_externalizations', {}, 'count')
        self._check('installed_version',
                    state['version'] == self.old
                    and state['status'] == 'Enabled',
                    f"Version={state['version']} (want {self.old}) "
                    f"Status={state['status']} "
                    f"Updatestatus={state['update_status']!r} "
                    f"Autoupdate={state['autoupdate']!r} "
                    f"errorCount={self.errors_before}")
        self._check('dialog_guard', bool(self._arm_dialogs()),
                    'a swap wipes the seeded _smoke_test_responses; staggered '
                    'run()s re-arm it so no modal can freeze TD')
        manifest, manifest_path = self._manifest()
        updates_dir = state['updates_dir']

        state = self._update(manifest, manifest_path, updates_dir, 'update',
                             before)
        fp_new = state['fingerprint']
        self._check('update_identity', fp_new != self.fp_old,
                    f'{self.fp_old[:12]}... -> {fp_new[:12]}... (the extension '
                    f"DATs are the new build's)")
        self._health('update_health', state)
        ext_after = self._tool('get_externalizations', {}, 'count')
        self._check('externalizations', ext_after == ext_before,
                    f'{ext_before} rows before, {ext_after} after')
        self._check('up_to_date',
                    self._py(_UPTODATE % (f'v{self.new}',), 30)
                    == f'Up to date (v{self.new})',
                    f'a check that sees v{self.new} as latest now reads '
                    f'up to date')

        rolled = self._rollback(updates_dir, state)

        # The re-update is what leaves the project on the NEW build for any
        # leg that follows, so it runs whenever the ceiling still allows a
        # full swap; a skip says so rather than reading as coverage.
        left = self.ctx['budget']() - _RESERVE_S
        if left < _RELOAD_TIMEOUT_S / 2:
            self._ok('reupdate', f'skipped -- {left:.0f}s of budget left, '
                                 f'needs {_RELOAD_TIMEOUT_S / 2:.0f}s; the '
                                 f'project is left on the pre-update build')
            return
        self._arm_dialogs()
        state = self._update(manifest, manifest_path, updates_dir, 'reupdate',
                             rolled)
        self._check('reupdate_identity', state['fingerprint'] == fp_new,
                    f"back to {fp_new[:12]}... (the build under test)")
        self._health('reupdate_health', state)
