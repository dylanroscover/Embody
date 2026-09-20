"""
Smoke leg `upgrade`: the installed PREVIOUS release self-updates to the build
under test, rolls back to the updater's own backup, then updates again.

The LEG makes no network call. UpdaterExt exposes no manifest/asset override,
so it seeds the updater's `_pending` slot (the dict a check would have
produced) from the repo's release/embody-release.json -- through the product's
own validateManifest gate -- and a copy of ctx['build']['tox'] staged in
.embody/updates, where a real download lands. Everything after that is the
product's own path: ApplyUpdate -> backup export -> in-place swap ->
VerifyUpdate, then the leftover-sentinel recovery -> _rollback ->
VerifyRollback. The RUN is not hermetic though: a fresh install ships
Autoupdate=notify, so every reloaded component runs its own api.github.com
check -- which is why each apply waits the updater's busy latch out and
retries a refusal that names it.

Four mechanisms the assertions are built on:
- Updatestatus is NOT a witness in either direction: the reloaded component
  runs its own notify-mode startup check within seconds and overwrites
  'Updated to vNEW' (and the rollback's line) with 'Up to date (vNEW)'
  (measured 2026-09-19). The swap also PRESERVES par values, so par.Version
  proves an update but never a rollback -- nothing stamps it back. Both
  directions are pinned instead on the EmbodyExt op id (the updater's own
  reload token), on the component's DATs, and on the sentinel file, which only
  VerifyUpdate / VerifyRollback delete, each after confirming the op id moved.
- Identity uses two different readings of the DATs because one cannot serve
  both directions. 'It is the other build' compares a per-DAT digest map (the
  static text/table DATs) restricted to those that held still across two
  pre-apply samples -- the log FIFO and the runtime tables move on their own,
  and a release can
  legitimately leave the extension sources untouched (v6.2.50 -> v6.2.51 did),
  so neither a fixed exclusion list nor the extension sources alone can decide
  it without a false red on a gate that blocks releases. 'It is the SAME build
  again' (rollback, re-update) compares the extension-source fingerprint,
  which is stable by construction.
- The swap REPLACES the COMP's contents, wiping the storage that holds
  _smoke_test_responses -- without it EmbodyExt._messageBox opens a REAL modal
  and freezes the smoke TD, so staggered run()s restore the seed across a swap.
  They restore it only while the store is ABSENT: re-storing a consumed answer
  would put 'Embody Update' -> 0 back in reach of _finishCheck's Install
  dialog, and the smoke would install a live GitHub release.
- An Envoy call must never swap its own host synchronously (issue #110): the
  rollback trigger is deferred as one run() string. ApplyUpdate is safe called
  directly (its phase 2 is 150 frames out).

The final re-update is what leaves the project on the NEW build for the legs
that follow, so a run with no budget left for it FAILS rather than reporting a
pass for a project sitting on the previous release.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

from . import _probe as P

_POLL_S = 2.0
_RELOAD_TIMEOUT_S = 180.0   # swap + boot chain + Envoy restart, generously
_RESERVE_S = 25.0           # left before the RUN's ceiling; teardown itself
                            # is not budget-gated (up to 2x QUIT_TIMEOUT_S)
_PROBE_TIMEOUT_S = 10.0
_SETTLE_S = 20.0            # entry probe, and post-swap Envoy/status settling
_BUSY_TIMEOUT_S = 90.0
_MIN_BACKUP_BYTES = 100_000  # fallback only; the live floor is probed
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
dats = {}
for d in emb.findChildren(type=td.DAT):
    if d.type not in ('text', 'table'):
        continue   # reading .text force-cooks a script/folder/file-in DAT
    try:
        t = d.text
    except Exception as e:
        t = repr(e)
    dats[d.path[len(emb.path):]] = hashlib.sha256(
        t.encode('utf-8')).hexdigest()[:12]
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
    'dats': dats,
    'updates_dir': str(u._updatesDir(True)),
    'min_backup': int(getattr(u, '_MIN_BACKUP_BYTES', 0)),
    'busy': bool(getattr(u, '_busy', False)),
    'busy_phase': str(getattr(u, '_busy_phase', '')),
    'ready': bool(emb.extensionsReady),
})
"""

# MERGES into whatever is stored: smoke_bootstrap seeded the install dialogs
# ('Embody - AI Coding Assistant Integration', the Convoy titles) and an
# overwrite would drop them for every boot chain after a swap. The staggered
# re-arms are RESTORATIVE (store absent only) -- see the module docstring.
_SEED = """
import td
emb = op.Embody
seed = dict(emb.fetch('_smoke_test_responses', None, search=False) or {})
seed.update(%r)
emb.store('_smoke_test_responses', dict(seed))
for f in list(range(20, 1801, 20)) + list(range(1920, 10801, 240)):
    td.run("op(args[1]).store('_smoke_test_responses', dict(args[0]))"
           " if op(args[1]) is not None and op(args[1]).fetch("
           "'_smoke_test_responses', None, search=False) is None else None",
           seed, emb.path, delayFrames=f)
result = emb.path
"""

# validateManifest is the product's gate on the manifest (asset traversal,
# size cap, custom_pars / builtin_pars types). Seeding _pending bypasses
# _finishCheck, which is where a real user meets it, so the leg runs it here:
# a shipped manifest the product would refuse must never install in the smoke.
_APPLY = """
import json
u = op.Embody.op('updater').ext.UpdaterExt
with open(%r, encoding='utf-8') as f:
    mf = json.load(f)
bad = u.validateManifest(mf)
if bad:
    result = json.dumps({'error': 'validateManifest refused the shipped '
                                  'manifest: ' + str(bad)})
else:
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
# reported as context, never waited on and never claimed as observed.
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
        installed = ctx.get('installed') or {}
        self.old = str(installed.get('version') or '')
        self.old_tox = installed.get('tox')
        self.new = str((ctx.get('build') or {}).get('version') or '')
        self.fp_old = ''
        self.errors_before = 0
        self.probe_error = ''
        self.sentinel_seen = False
        self.run_dir = os.path.realpath(str(ctx['run_dir']))

    # ---- steps -----------------------------------------------------------

    def _ok(self, step, detail=''):
        self.steps.append({'step': step, 'ok': True, 'detail': detail})

    def _fail(self, step, detail):
        self.steps.append({'step': step, 'ok': False, 'detail': detail})
        raise _Abort(f'{step}: {detail}')

    def _check(self, step, ok, detail):
        (self._ok if ok else self._fail)(step, detail)

    def _installed(self, version, tox):
        """The legs that follow inherit ctx and describe the build they think
        is running, so it tracks every swap -- in both directions."""
        self.ctx['installed'] = {'tox': tox, 'version': version}

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
        # samefile, not a string compare: realpath resolves symlinks but not
        # case, so a re-cased folder reads as another project's and the miss
        # surfaces as a connectivity error (the class CI hit 2026-09-20).
        if not P.same_path(str(state.get('folder')), self.run_dir):
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

    def _entry_probe(self, timeout=_SETTLE_S):
        """The leg's first read polls like every other one: the MCP probe
        phase that runs just before it can leave Envoy mid-restart, and one
        transient miss must not abort the leg."""
        deadline = self.clock() + max(
            0.0, min(timeout, self.ctx['budget']() - _RESERVE_S))
        while True:
            state = self._probe()
            if state is not None:
                return state
            if self.clock() >= deadline:
                return None
            self._reconnect()
            self.sleep(_POLL_S)

    def _wait(self, step, want, detail, timeout=_RELOAD_TIMEOUT_S, watch=None):
        """Poll the component until `want(state)`, re-reading the port each
        round. Fails the step (and the leg) on timeout or an empty budget.
        `watch` runs every round, answered or not."""
        window = min(timeout, max(0.0, self.ctx['budget']() - _RESERVE_S))
        if window <= 0:
            self._fail(step, f'{detail}: no budget left in the run')
        deadline = self.clock() + window
        last = None
        while self.clock() < deadline:
            if watch is not None:
                watch()
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

    def _apply(self, tag, manifest_path, staged):
        """Wait the updater's busy latch out, then apply; returns the last
        pre-apply state. The wait and the call are two round trips and the
        reloaded component's notify-mode startup check can re-arm the latch
        between them, so a refusal naming it is retried rather than reported
        (measured 2026-09-19: the re-update fired ~1s after the rollback and
        was refused)."""
        reply = {}
        for attempt in range(3):
            pre = self._wait(f'apply_{tag}', lambda s: not s['busy'],
                             'the updater never left its busy phase',
                             timeout=_BUSY_TIMEOUT_S)
            reply = json.loads(self._py(_APPLY % (manifest_path, staged), 30))
            if reply.get('status') == 'applying':
                self._ok(f'apply_{tag}',
                         f'ApplyUpdate(interactive=False) -> {reply}'
                         + (f' (attempt {attempt + 1})' if attempt else ''))
                return pre
            if 'already running' not in str(reply.get('error') or ''):
                break
            self.sleep(_POLL_S)
        self._fail(f'apply_{tag}', f'ApplyUpdate(interactive=False) -> {reply}')

    def _update(self, manifest, manifest_path, updates_dir, tag, was):
        """Seed _pending and drive one real ApplyUpdate to a verified new
        version. `was` is the state before the apply: a new EmbodyExt op id
        (the updater's own reload token) says the swap happened, par.Version
        says VerifyUpdate accepted it, and the cleared sentinel says it
        finished."""
        staged = self._stage(updates_dir, manifest, f'stage_{tag}')
        pre = self._apply(tag, manifest_path, staged)
        t0 = self.clock()
        self.sentinel_seen = False
        state = self._wait(f'verify_{tag}',
                           lambda s: (s['token'] != was['token']
                                      and s['version'] == self.new
                                      and s['ready']
                                      and self._sentinel_cleared(updates_dir)),
                           f'the component never became a verified v{self.new}',
                           watch=lambda: self._note_sentinel(updates_dir))
        self._installed(self.new, self.ctx['build']['tox'])
        self._ok(f'verify_{tag}',
                 f"Version={state['version']} EmbodyExt id {was['token']} -> "
                 f"{state['token']} after {self.clock() - t0:.0f}s on port "
                 f"{self.port}; sentinel {self._sentinel_note()}; "
                 f"Updatestatus={state['update_status']!r}")
        self._moved(f'{tag}_identity', was, pre, state)
        self._health(f'{tag}_health', state)
        return state

    def _sentinel_cleared(self, updates_dir):
        """VerifyUpdate / VerifyRollback delete pending.json only after each
        confirms a real reload -- the one race-free witness on disk."""
        return not os.path.isfile(os.path.join(updates_dir, 'pending.json'))

    def _note_sentinel(self, updates_dir):
        """Evidence, not a gate: the sentinel's life overlaps the window where
        Envoy is down, so 'never seen' is normal on a fast swap and must not
        fail a step."""
        if not self._sentinel_cleared(updates_dir):
            self.sentinel_seen = True

    def _sentinel_note(self):
        return 'seen, then cleared' if self.sentinel_seen else 'cleared'

    def _moved(self, step, pre_a, pre_b, after):
        """The DATs are the other build's. Only the ones that held identical
        across the two pre-apply samples are compared -- see the module
        docstring on why neither an exclusion list nor the extension sources
        alone can decide this."""
        first = pre_a.get('dats') or {}
        second = pre_b.get('dats') or {}
        now = after.get('dats') or {}
        stable = {p: h for p, h in first.items() if second.get(p) == h}
        changed = sorted(p for p, h in stable.items() if now.get(p) != h)
        appeared = sorted(set(now) - set(second))
        gone = sorted(set(second) - set(now))
        self._check(step, bool(changed or appeared or gone),
                    f'{len(changed)}/{len(stable)} stable DATs changed, '
                    f'{len(appeared)} appeared, {len(gone)} gone '
                    f'{(changed + appeared + gone)[:3]}')

    def _health(self, step, state):
        """Envoy and Status settle a beat after the probe answers -- EnvoyExt
        writes 'Running on port N' only once the bind is confirmed, and a
        watchdog revive can land after a swap -- so the predicate polls like
        every other read instead of hard-failing one sample."""
        def healthy(s):
            return (s['status'] == 'Enabled'
                    and s['envoy'] == f'Running on port {self.port}')

        if not healthy(state):
            state = self._wait(step, healthy,
                               'the component never settled to Status=Enabled '
                               'with Envoy running', timeout=_SETTLE_S)
        errors = self._errors()
        self._check(step, errors is not None and errors <= self.errors_before,
                    f"Status={state['status']} errorCount={errors} "
                    f"(was {self.errors_before}) "
                    f"Envoystatus={state['envoy']!r}")

    def _rollback(self, updates_dir, was):
        """Drive the user-visible recovery: a leftover sentinel naming the
        backup, then CheckForUpdate -> 'Restore Backup' -> _rollback."""
        backup = os.path.join(updates_dir, f'backup-v{self.old}.tox')
        size = os.path.getsize(backup) if os.path.isfile(backup) else 0
        floor = int(was.get('min_backup') or _MIN_BACKUP_BYTES)
        self._check('backup', size >= floor,
                    f'{backup} {size} bytes (floor {floor}; a fresh '
                    f'ExportPortableTox of the live v{self.old} COMP, so its '
                    f'sha256 is deliberately NOT the installed tox\'s)')
        sentinel = {'from_version': self.old, 'to_version': self.new,
                    'tag': f'v{self.new}', 'tox_path': '',
                    'backup_path': backup.replace('\\', '/'),
                    'backup_sha256': _sha256(backup), 'phase': 'reloading',
                    'session': {'pid': 0, 'started': 0}}
        # 'Embody Update' -> button 0 IS the Restore Backup answer; the
        # sentinel key keeps the store alive after it is consumed. Deliberately
        # NOT seeded: StartupCheck's own 'Embody Update Recovery' prompt, which
        # this forged (pid 0) sentinel can raise -- -1 leaves the sentinel for
        # VerifyRollback, while its button 1 would steal it.
        self._arm_dialogs({'Embody Update': 0})
        self.sentinel_seen = False
        self._check('rollback_trigger',
                    self._py(_ROLLBACK_ARM % (json.dumps(sentinel),), 30)
                    == 'armed',
                    f'leftover sentinel written; CheckForUpdate deferred '
                    f'(backup {os.path.basename(backup)})')
        rolled = self._wait('rollback',
                            lambda s: (s['token'] != was['token']
                                       and s['ready']
                                       and self._sentinel_cleared(updates_dir)),
                            'the backup was never reloaded',
                            watch=lambda: self._note_sentinel(updates_dir))
        self._installed(self.old, self.old_tox)
        self._ok('rollback',
                 f"EmbodyExt id {was['token']} -> {rolled['token']}; sentinel "
                 f"{self._sentinel_note()} (VerifyRollback writes "
                 f"{_ROLLED_BACK!r} there, and the startup check overwrites it "
                 f"within seconds, so it is not sampled -- Updatestatus now "
                 f"{rolled['update_status']!r}); par.Version reads "
                 f"{rolled['version']} -- the swap preserves par values and "
                 f"nothing stamps them back")
        self._check('rollback_identity', rolled['fingerprint'] == self.fp_old,
                    f"extension sources {rolled['fingerprint'][:12]}... == the "
                    f"pre-update {self.fp_old[:12]}...")
        self._health('rollback_health', rolled)
        return rolled

    # ---- the leg ---------------------------------------------------------

    def execute(self):
        if not self.new or not self.old or self.old == self.new:
            self._fail('preconditions',
                       f'nothing to upgrade: installed v{self.old!r}, build '
                       f'v{self.new!r} -- --legs upgrade stages the previous '
                       f'release before boot')
        state = self._entry_probe()
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
                    'run()s restore it so no modal can freeze TD')
        manifest, manifest_path = self._manifest()
        updates_dir = state['updates_dir']

        state = self._update(manifest, manifest_path, updates_dir, 'update',
                             before)
        fp_new = state['fingerprint']
        ext_after = self._tool('get_externalizations', {}, 'count')
        self._check('externalizations',
                    ext_before is not None and ext_after == ext_before,
                    f'{ext_before} rows before, {ext_after} after (None means '
                    f'the table was never read)')
        self._check('up_to_date',
                    self._py(_UPTODATE % (f'v{self.new}',), 30)
                    == f'Up to date (v{self.new})',
                    f'a check that sees v{self.new} as latest now reads '
                    f'up to date')

        rolled = self._rollback(updates_dir, state)

        # The re-update is what leaves the project on the NEW build for the
        # legs that follow, so no budget for it FAILS the leg: passing here
        # would report the gate green for a project sitting on the previous
        # release, with `faults` and `uninstall` testing that one.
        left = self.ctx['budget']() - _RESERVE_S
        if left < _RELOAD_TIMEOUT_S / 2:
            self._fail('reupdate',
                       f'no time to put the build under test back: {left:.0f}s '
                       f'left, needs {_RELOAD_TIMEOUT_S / 2:.0f}s. The project '
                       f'is on the pre-update v{self.old}, so anything after '
                       f'this would test the wrong build -- raise the run '
                       f'ceiling (smoke_run.py --timeout)')
        self._arm_dialogs()
        state = self._update(manifest, manifest_path, updates_dir, 'reupdate',
                             rolled)
        self._check('build_restored', state['fingerprint'] == fp_new,
                    f"extension sources back to {fp_new[:12]}... (the build "
                    f"under test)")
