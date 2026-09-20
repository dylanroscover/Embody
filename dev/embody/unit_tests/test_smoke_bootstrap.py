"""
Test suite: the IN-TD half of the fresh-install smoke (smoke_bootstrap.py),
driven on a frame-scheduled fake TouchDesigner.

smoke_bootstrap.py never ran under a test before: everything in it is
scheduled through TD's run(code, *args, delayFrames=N) and reads the TD
globals (op / me / project / ui), so the only way to exercise it was a
20-minute round trip on a CI runner. This fake supplies those globals and
a tick loop, with FRAMES PER SECOND AS A PARAMETER -- a frame count is not
a wall-clock deadline, and the two macOS CI failures of 2026-09-20 were
exactly that mistake:

- the headless wizard applied at a fixed delayFrames=55 and landed INSIDE
  Embody's init on a slow boot;
- init had set Envoyenable=False to stop an auto-start, TD DEFERS that
  onValueChange, and the callback ran AFTER the wizard enabled Envoy --
  Stop() then held the fresh install Disabled for the whole run
  (execute.py:19-25 documents the hazard).

What this pins:
- the headless setup waits for Embody's _init_complete, never a frame
  count, at 60fps and at 12fps, with init landing early and late;
- a deferred Envoyenable=False landing after the setup is re-asserted, so
  the install does not come up Disabled (the bug is reproduced first, with
  the re-assert disabled, and the same drive then comes up Running);
- ready.flag: one atomic write, the stamps smoke_run.parse_ready reads,
  and a FAIL when Envoy is not running;
- features.flag: PENDING while legs are outstanding, every leg PASS at the
  end, SKIP only as "not reached" (which smoke_run counts as a failure);
- every dialog the bootstrap can raise is auto-answered and the sentinel
  keeps the store alive, so nothing falls through to a blocking modal;
- the abort path writes a FAIL flag instead of burning the orchestrator's
  whole ready budget.

Pure Python, TD-import-free: inside TouchDesigner every test skips.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_IN_TD = 'td' in sys.modules

_DEV = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_RT = os.path.join(_DEV, 'release_testing')

# The module under test. The env var lets a mutation run point this suite
# at a weakened COPY without editing a line of it.
BOOTSTRAP_PATH = os.environ.get('EMBODY_SMOKE_BOOTSTRAP',
                                os.path.join(_RT, 'smoke_bootstrap.py'))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if not _IN_TD:
    # The orchestrator's parsers ARE the contract for both flags: asserting
    # through them is what stops the two halves drifting apart.
    smoke = _load('smoke_run_for_bootstrap_tests',
                  os.path.join(_RT, 'smoke_run.py'))


# ===========================================================================
# the fake TouchDesigner
# ===========================================================================

class _Type:
    """A stand-in for baseCOMP / noiseTOP / nullTOP."""

    def __init__(self, name):
        self.name = name


class _Par:
    def __init__(self, pars, name):
        self._pars, self._name = pars, name

    def eval(self):
        return self._pars.get(self._name)

    def pulse(self, *a, **k):
        self._pars.pulse(self._name)

    def __str__(self):
        return str(self.eval())


class _FreePars:
    """Any parameter, no callbacks -- the ops the feature legs build."""

    def __init__(self, comp, values=None):
        object.__setattr__(self, '_comp', comp)
        object.__setattr__(self, '_vals', dict(values or {}))

    def get(self, name):
        return object.__getattribute__(self, '_vals').get(name)

    def pulse(self, name):
        object.__getattribute__(self, '_comp').pulses.append(name)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return _Par(self, name)

    def __setattr__(self, name, value):
        object.__getattribute__(self, '_vals')[name] = value


class _EmbodyPars(_FreePars):
    """Embody's custom pars: a fixed set (an unknown name must raise, the
    way _par() expects), and every write defers its onValueChange."""

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        if name not in object.__getattribute__(self, '_vals'):
            raise AttributeError(name)
        return _Par(self, name)

    def __setattr__(self, name, value):
        object.__getattribute__(self, '_comp').writePar(name, value)


class _Connector:
    def __init__(self, owner):
        self.owner, self.connected = owner, []

    def connect(self, other):
        self.connected.append(other)


class _FakeOP:
    def __init__(self, td, parent, name, kind=None):
        self.td, self.parent_op, self.name = td, parent, name
        self.kind = kind
        self.kids = {}
        self.pulses = []
        self.nodeX = self.nodeY = 0
        self.nodeWidth = self.nodeHeight = 100
        self.inputConnectors = [_Connector(self)]
        self.par = _FreePars(self)
        self.valid = True
        self._storage = {}
        self.module = None

    @property
    def path(self):
        if self.parent_op is None:
            return '/'
        base = self.parent_op.path
        return ('/' + self.name) if base == '/' else base + '/' + self.name

    # --- children ----------------------------------------------------
    def create(self, kind, name):
        child = _FakeOP(self.td, self, name, kind)
        self.kids[name] = child
        self.td.index[child.path] = child
        return child

    def op(self, name):
        return self.kids.get(str(name).lstrip('./'))

    @property
    def children(self):
        return list(self.kids.values())

    def destroy(self):
        self.valid = False
        for kid in list(self.kids.values()):
            kid.destroy()
        if self.parent_op is not None:
            self.parent_op.kids.pop(self.name, None)
        self.td.index.pop(self.path, None)

    # --- storage -----------------------------------------------------
    def store(self, key, value):
        self._storage[key] = value

    def fetch(self, key, default=None, search=True):
        return self._storage.get(key, default)

    def unstore(self, key):
        self._storage.pop(key, None)

    def errors(self, recurse=False):
        return ''

    def scriptErrors(self, recurse=False):
        return ''


class _FakeEmbody(_FakeOP):
    """The Embody COMP as the bootstrap can observe it: custom pars with
    DEFERRED onValueChange, storage, a wizard window, the extension entry
    points the feature legs call, and a settable init that stores
    _init_complete at a chosen wall-clock moment."""

    def __init__(self, td, parent, name):
        super().__init__(td, parent, name)
        self.par = _EmbodyPars(self, {
            'Envoyenable': False, 'Envoystatus': 'Disabled',
            'Status': 'Initializing', 'Version': '6.2.57',
            'Updatestatus': 'Up to date', 'Autosavestatus': 'Idle',
            'Filecleanup': 'delete', 'Clipboardautopaste': True,
            'Convoyenable': False, 'Convoystatus': 'Disabled',
            'Envoyport': 9871, 'w': 300,
        })
        self.par_writes = []
        self.applied = []          # (frame, kwargs) per _applyWizardSetup
        self.dialogs = []          # (title, answer)
        self.blocked = []          # titles that reached ui.messageBox
        self.convoy_seed = None    # the store as it was at Convoyenable
        self.stop_frames = []      # every Stop() a deferred callback ran
        self.init_frame = None
        self._envoy_gen = 0
        self._wizard = self.create(_Type('windowCOMP'), 'window_wizard')
        self.ext = _Ext(self)
        self._buildViz()

    # --- parameters ---------------------------------------------------
    def writePar(self, name, value, defer=None):
        self.par._vals[name] = value
        self.par_writes.append((self.td.frame, name, value))
        # TD defers onValueChange to a later cook, carrying the value as it
        # was at WRITE time. parexec DROPS a callback that lands before
        # _init_complete (parexec.py:18) -- so init's own write only bites
        # when its deferral outlives init, which is the whole hazard.
        frames = self.td.defer_frames if defer is None else defer
        self.td.after(frames,
                      lambda n=name, v=value: self._onParChange(n, v))

    def _onParChange(self, name, value):
        if not self.fetch('_init_complete', False, search=False):
            return              # parexec is suppressed until init finishes
        if name == 'Envoyenable':
            self._startEnvoy() if value else self._stopEnvoy()
        elif name == 'Convoyenable' and value:
            self._enableConvoy()

    def _startEnvoy(self):
        self._envoy_gen += 1
        gen = self._envoy_gen
        self.par._vals['Envoyenable'] = True
        if self.td.envoy_start_fails:
            self.par._vals['Envoystatus'] = (
                'Envoy start aborted -- dependency install failed')
            return
        self.par._vals['Envoystatus'] = 'Installing deps... (one-time)'

        def running():
            if gen != self._envoy_gen:
                return          # a stop landed in between
            self.par._vals['Envoystatus'] = 'Running on port 9871'
            self.td.writeFile('.mcp.json', '{"mcpServers": {"envoy": {}}}')
        self.td.after(self.td.envoy_start_frames, running)

    def _stopEnvoy(self):
        # Stop() -- what the stale init callback runs. No new callback: the
        # product's Stop writes the par back itself.
        self._envoy_gen += 1
        self.stop_frames.append(self.td.frame)
        self.par._vals['Envoyenable'] = False
        self.par._vals['Envoystatus'] = 'Disabled'

    def _enableConvoy(self):
        self.convoy_seed = dict(self.fetch('_smoke_test_responses', {}) or {})
        self.par._vals['Convoystatus'] = 'Installing host app...'
        if self.td.convoy_verdict is None:
            return              # never terminal: the poll must time out
        self.td.after(self.td.convoy_frames,
                      lambda: self.par._vals.__setitem__(
                          'Convoystatus', self.td.convoy_verdict))

    # --- init ----------------------------------------------------------
    def beginInit(self):
        """Embody's own startup, in the two moments the bootstrap races:
        init disables Envoy to stop an auto-start (a DEFERRED callback),
        and stores _init_complete when its settings restore has run."""
        td = self.td
        self.par._vals['Status'] = 'Scanning defaults (40/688)'
        if td.stale_disable_at_s is not None:
            td.afterSeconds(td.stale_disable_at_s, lambda: self.writePar(
                'Envoyenable', False, defer=td.stale_defer_frames))
        if td.init_complete_at_s is None:
            return

        def done():
            self.init_frame = td.frame
            self.store('_init_complete', True)
            self.par._vals['Status'] = 'Enabled'
        td.afterSeconds(td.init_complete_at_s, done)

    # --- dialogs --------------------------------------------------------
    def messageBox(self, title, buttons=None):
        """EmbodyExt._messageBox, spelled as it behaves: a seeded answer is
        consumed, the key is dropped when exhausted, and the store is
        unstored when no keys remain -- after which the NEXT dialog falls
        through to a real, blocking ui.messageBox."""
        responses = self.fetch('_smoke_test_responses', None, search=False)
        if responses is not None and title in responses:
            choice = responses.pop(title)
            if not responses:
                self.unstore('_smoke_test_responses')
            self.dialogs.append((title, choice))
            return choice
        if responses is not None:
            self.dialogs.append((title, -1))
            return -1
        self.blocked.append(title)
        return self.td.ui.messageBox(title, '', buttons=buttons)

    # --- the viz_status readout the feature leg checks -------------------
    def _buildViz(self):
        viz = self.create(_Type('containerCOMP'), 'viz_status')
        viz.par.w = 300
        pub = viz.create(_Type('textDAT'), 'status_publish')
        table = viz.create(_Type('tableDAT'), 'status_table')
        table.rows = [['name', 'value']]
        table.numRows = 1
        prog = self.create(_Type('textDAT'), 'startup_progress')

        def table_rows(embody, width, now=0.0):
            return [('row%d' % i, 'v%d' % i) for i in range(19)]
        prog.module = _Mod(table_rows=table_rows)

        def refresh():
            rows = prog.module.table_rows(self, viz.par.w.eval(), now=0.0)
            table.rows = [['name', 'value']] + [list(r) for r in rows]
            table.numRows = len(table.rows)
        pub.module = _Mod(Refresh=refresh)
        table.__class__ = _TableOP


class _TableOP(_FakeOP):
    def __getitem__(self, key):
        row, col = key
        header = self.rows[0]
        return self.rows[row][header.index(col)]


class _Mod:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Ext:
    """embody.ext.<Name> -- only what the bootstrap calls."""

    def __init__(self, embody):
        self.Embody = _EmbodyExt(embody)
        self.TDXN = _TDXNExt(embody)


class _EmbodyExt:
    def __init__(self, embody):
        self.emb = embody
        self.tagged = []
        self.tracked = {}

    @property
    def Externalizations(self):
        table = _FakeOP(self.emb.td, None, 'externalizations')
        table.numRows = 1 + len(self.tracked)
        return table

    def _applyWizardSetup(self, **kwargs):
        self.emb.applied.append((self.emb.td.frame, kwargs))
        if self.emb.td.wizard_setup_raises:
            raise RuntimeError('wizard setup failed')
        self.emb.par.Envoyenable = True

    def applyTagToOperator(self, oper, tag):
        self.tagged.append((oper.path, tag))
        return True

    def _gateOpen(self):
        td = self.emb.td
        return os.path.isfile(os.path.join(td.project.folder,
                                           td.project.name))

    def externalizeImmediate(self, oper):
        if not self._gateOpen():
            return              # deferred behind the on-disk gate
        rel = 'embody/%s.tdn' % oper.name
        self.tracked[oper.path] = rel
        self._writeTDN(oper, rel)

    def _writeTDN(self, oper, rel):
        path = str(self.buildAbsolutePath(rel))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        kids = oper.children
        text = '\n'.join(
            k.name + ('' if k.par.period.eval() is None
                      else ' period=%s' % k.par.period.eval())
            for k in kids)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text + '\n')

    def _getStrategyFilePath(self, op_path, strategy):
        return self.tracked.get(op_path)

    def buildAbsolutePath(self, rel):
        return os.path.join(self.emb.td.run_dir, str(rel))

    def saveTDXN(self, op_path, **kw):
        rel = self.tracked.get(op_path)
        if rel:
            self._writeTDN(self.emb.td.index[op_path], rel)
        return True

    def checkpoint(self, op_path):
        if self.emb.fetch('_suppress_dialogs', False, search=False):
            return False        # the save window -- Checkpoint refuses
        if self.emb.td.checkpoint_fails:
            return False
        self.emb.par._vals['Autosavestatus'] = 'Saved (checkpoint) 0s ago'
        return True

    def ExportPortableTox(self, target=None, save_path=None):
        if self.emb.td.portable_error:
            raise RuntimeError(self.emb.td.portable_error)
        with open(save_path, 'wb') as f:
            f.write(b'TOX' * 64)
        self.emb.td.portable[os.path.basename(save_path)] = [
            k.name for k in target.children]
        return True


class _TDXNExt:
    def __init__(self, embody):
        self.emb = embody

    def importNetworkFromFile(self, file_path, target_path, clear_first=False):
        comp = self.emb.td.index[target_path]
        with open(file_path, encoding='utf-8') as f:
            text = f.read()
        for line in text.splitlines():
            if not line.strip():
                continue
            name, _, period = line.partition(' period=')
            kid = comp.op(name) or comp.create(_Type('TOP'), name)
            if period:
                kid.par.period = float(period)
        return {'ok': True, 'operators': len(text.splitlines())}


class _FakeTD:
    """The scheduler and the globals. Frames are the clock the bootstrap
    schedules on; FPS converts them to the wall time Embody's init and
    Envoy's venv bootstrap actually take, so a frame count used as a
    deadline reads differently on a slow machine -- which is the point."""

    def __init__(self, run_dir, fps=60):
        self.run_dir = run_dir
        self.fps = fps
        self.frame = 0
        self.queue = []          # (due_frame, seq, fn)
        self.seq = 0
        self.index = {}
        self.logs = []
        self.portable = {}
        # knobs
        self.defer_frames = 2            # TD's onValueChange deferral
        self.stale_defer_frames = 60     # init's own write, deferred past it
        self.init_complete_at_s = self.secs(50)   # _init_complete lands
        self.stale_disable_at_s = None   # init's Envoyenable=False write
        self.envoy_start_frames = 30
        self.envoy_start_fails = False
        self.wizard_setup_raises = False
        self.checkpoint_fails = False
        self.portable_error = None       # a leg exception with a newline
        self.convoy_frames = 60
        self.convoy_verdict = 'Connected (host app 1.4.0)'
        self.loadtox_raises = False
        self.loadtox_none = False
        self.dialogs_at = (31, 41, 47)   # dup, Envoy opt-in, git prompt
        self.save_window_frames = 120
        self.save_delay_frames = 0       # how late the .toe lands on disk

        self.root = _FakeOP(self, None, '')
        self.index['/'] = self.root
        self.me = _FakeOP(self, None, 'execute')
        self.project = _Project(self)
        self.ui = _UI(self)
        self.absTime = _Mod(seconds=0.0, frame=0)
        self.embody = None

    # --- scheduling ----------------------------------------------------
    def after(self, frames, fn):
        self.seq += 1
        self.queue.append((self.frame + max(int(frames), 0), self.seq, fn))
        self.queue.sort(key=lambda row: (row[0], row[1]))

    def afterSeconds(self, seconds, fn):
        self.after(int(round(float(seconds) * self.fps)), fn)

    def run(self, code, *args, **kw):
        """TD's run(): the code string is executed with `args` in scope."""
        frames = kw.get('delayFrames')
        if frames is None:
            ms = kw.get('delayMilliSeconds')
            frames = (float(ms) / 1000.0 * self.fps) if ms else 0
        self.after(frames, lambda: exec(code, {'args': list(args)}))

    def tick(self, frames=1):
        for _ in range(int(frames)):
            self.frame += 1
            self.absTime.seconds = self.frame / float(self.fps)
            self.absTime.frame = self.frame
            while self.queue and self.queue[0][0] <= self.frame:
                self.queue.pop(0)[2]()

    def drive(self, until=None, max_frames=40000):
        for _ in range(int(max_frames)):
            if until is not None and until():
                return True
            if until is None and not self.queue:
                return True
            self.tick()
        return until is None or bool(until and until())

    def secs(self, frames):
        """Wall time for a frame count ON THIS MACHINE -- the conversion the
        bootstrap must never bake in."""
        return float(frames) / float(self.fps)

    @property
    def seconds(self):
        return self.frame / float(self.fps)

    # --- the op() global ------------------------------------------------
    def op(self, path=None):
        if path is None:
            return None
        got = self.index.get(str(path))
        return got if (got is not None and got.valid) else None

    def loadTox(self, parent, tox_path):
        if self.loadtox_raises:
            raise RuntimeError('tox is corrupt')
        if self.loadtox_none:
            return None
        base = os.path.basename(str(tox_path))
        if base in self.portable:
            comp = parent.create(_Type('baseCOMP'), 'smoke_portable')
            for name in self.portable[base]:
                comp.create(_Type('TOP'), name)
            return comp
        self.embody = _FakeEmbody(self, parent, 'Embody')
        parent.kids['Embody'] = self.embody
        self.index[self.embody.path] = self.embody
        for kid in self.embody.children:
            self.index[kid.path] = kid
        self.embody.beginInit()
        for at, title in zip(self.dialogs_at, (
                'Embody', 'Embody - AI Coding Assistant Integration',
                'Envoy -- Git Repository Recommended')):
            self.after(at, lambda t=title: self.embody.messageBox(t))
        return self.embody

    def writeFile(self, rel, text):
        path = os.path.join(self.project.folder, rel)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)


class _Project:
    def __init__(self, td):
        self.td = td
        self.folder = td.run_dir
        # What TD reports on a pristine template copy: the NEXT incremental
        # name, while the disk holds smoke_template.toe. So the gate path
        # (folder/project.name) does not exist and the gate is shut.
        self.name = 'smoke_template.4.toe'
        self.saves = []
        self.increment = 4

    def save(self, path=None):
        self.saves.append(path)
        if path is None:
            self.increment += 1      # a bare save auto-increments
            path = os.path.join(self.folder,
                                'smoke_template.%d.toe' % self.increment)
        dest = path
        # The file is what opens Embody's on-disk gate, and a real save does
        # not land the instant save() returns.
        self.td.after(self.td.save_delay_frames,
                      lambda: open(dest, 'wb').write(b'toe'))
        emb = self.td.embody
        if emb is None:
            return
        # The save window: execute.py sets _suppress_dialogs on PreSave and
        # only clears it 120 frames after the post-save restore.
        emb.store('_suppress_dialogs', True)
        emb.par._vals['Autosavestatus'] = 'Saved 0s ago'
        self.td.after(self.td.save_window_frames,
                      lambda: emb.unstore('_suppress_dialogs'))


class _UI:
    """A real ui.messageBox in an unattended run IS the failure: it blocks
    the frame loop until a human clicks."""

    def __init__(self, td):
        self.td = td
        self.opened = []

    def messageBox(self, title, message, buttons=None):
        self.opened.append(title)
        return 0


# ===========================================================================
# the case
# ===========================================================================

class _Case(EmbodyTestCase):

    FPS = 60

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only')
        self.root = tempfile.mkdtemp(prefix='embody_smoke_boot_')
        self.repo = os.path.join(self.root, 'repo')
        self.run_dir = os.path.join(self.root, 'run')
        os.makedirs(os.path.join(self.repo, 'release'))
        os.makedirs(self.run_dir)
        self.tox = os.path.join(self.repo, 'release', 'Embody-v6.2.57.tox')
        with open(self.tox, 'wb') as f:
            f.write(b'TOX')
        with open(os.path.join(self.repo, 'release', 'embody-release.json'),
                  'w', encoding='utf-8') as f:
            json.dump({'version': '6.2.57',
                       'asset': 'Embody-v6.2.57.tox'}, f)
        self.sidecar = {'repo_root': self.repo, 'tox_path': self.tox,
                        'flags_dir': self.run_dir, 'run_id': 'windows-7',
                        'platform': 'win32'}
        self._sidecar()
        with open(os.path.join(self.run_dir, 'smoke_template.toe'), 'wb') as f:
            f.write(b'toe')     # the pristine template the cleanup keeps

        self.td = _FakeTD(self.run_dir, fps=self.FPS)
        self.boot = _load('smoke_bootstrap_under_test_%d' % id(self),
                          BOOTSTRAP_PATH)
        for name, value in (('run', self.td.run), ('op', self.td.op),
                            ('me', self.td.me), ('project', self.td.project),
                            ('ui', self.td.ui), ('absTime', self.td.absTime),
                            ('baseCOMP', _Type('baseCOMP')),
                            ('noiseTOP', _Type('noiseTOP')),
                            ('nullTOP', _Type('nullTOP'))):
            setattr(self.boot, name, value)
        # _log prints; its bootstrap.log mirror only starts once flags_dir is
        # stored, so the earliest warnings live nowhere else.
        self.boot.print = self.td.logs.append
        self.td.root.loadTox = lambda p: self.td.loadTox(self.td.root, p)

        # Every atomic write, in order: (basename, text). The wrapper also
        # proves the flag really goes through tmp + os.replace.
        self.writes = []
        self._real_replace = os.replace
        patcher = mock.patch('os.replace', side_effect=self._replace)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _replace(self, src, dst):
        try:
            with open(src, encoding='utf-8') as f:
                text = f.read()
        except OSError:
            text = ''
        self.writes.append((os.path.basename(str(dst)), str(src), text))
        return self._real_replace(src, dst)

    def _sidecar(self, **over):
        cfg = dict(self.sidecar)
        cfg.update(over)
        with open(os.path.join(self.run_dir, 'smoke_run.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(cfg, f)

    def tearDown(self):
        root = getattr(self, 'root', '')
        if not _IN_TD and root and 'embody_smoke_boot_' in root:
            shutil.rmtree(root, ignore_errors=True)
        super().tearDown()

    # --- helpers --------------------------------------------------------
    def start(self):
        self.boot.onStart()

    def flag(self, name):
        path = os.path.join(self.run_dir, name)
        if not os.path.isfile(path):
            return None
        with open(path, encoding='utf-8') as f:
            return f.read()

    def ready(self):
        text = self.flag('ready.flag')
        return smoke.parse_ready(text) if text is not None else None

    def features(self):
        text = self.flag('features.flag')
        return smoke.parse_features(text) if text is not None else None

    def wrote(self, name):
        return [w for w in self.writes if w[0] == name]

    def runToReady(self, max_frames=20000):
        self.start()
        self.td.drive(until=lambda: self.flag('ready.flag') is not None,
                      max_frames=max_frames)
        return self.ready()

    def runToEnd(self, max_frames=60000):
        self.start()
        self.td.drive(until=lambda: (self.features() or {}).get('terminal'),
                      max_frames=max_frames)
        return self.ready(), self.features()


# ===========================================================================
# 1. the headless setup waits for init, not for a frame count
# ===========================================================================

class TestHeadlessSetupWaitsForInit(_Case):

    def _driveToSetup(self, max_frames=6000):
        self.start()
        self.td.drive(until=lambda: bool(self.td.embody
                                         and self.td.embody.applied),
                      max_frames=max_frames)
        return self.td.embody

    def test_setup_lands_after_init_completes(self):
        """The fixed delayFrames=55 fired at ~frame 56; init completes at
        2.0s = frame 120 here, so a frame-count apply lands INSIDE init."""
        self.td.init_complete_at_s = 2.0
        emb = self._driveToSetup()
        self.assertTrue(emb.applied, 'the headless setup never applied')
        self.assertIsNotNone(emb.init_frame)
        self.assertGreaterEqual(
            emb.applied[0][0], emb.init_frame,
            'the setup applied at frame %s, before _init_complete at %s'
            % (emb.applied[0][0], emb.init_frame))

    def test_setup_lands_after_init_when_init_is_early(self):
        self.td.init_complete_at_s = 0.2
        emb = self._driveToSetup()
        self.assertGreaterEqual(emb.applied[0][0], emb.init_frame)

    def test_setup_lands_after_a_very_late_init(self):
        """A loaded machine: init still running seven seconds in."""
        self.td.init_complete_at_s = 7.0
        emb = self._driveToSetup()
        self.assertGreaterEqual(emb.applied[0][0], emb.init_frame)
        self.assertGreater(emb.applied[0][0], 55)

    def test_the_wizard_choices_are_the_ones_a_smoke_needs(self):
        emb = self._driveToSetup()
        kwargs = emb.applied[0][1]
        self.assertEqual(kwargs['mode'], 'auto')
        self.assertEqual(kwargs['assistant'], 'claudecode')
        self.assertEqual(kwargs['git'], 'gitskip')
        self.assertEqual(kwargs['externalize'], 'skip')

    def test_the_setup_applies_exactly_once(self):
        """The retry loop re-enters and a stale incremental save can carry
        headless_setup_done back in; the apply itself must not repeat --
        a second _applyWizardSetup re-enables Envoy over a live server."""
        emb = self._driveToSetup()
        self.td.tick(3000)
        self.assertEqual(len(emb.applied), 1, emb.applied)
        self.boot._apply_headless_setup()      # a re-entry, by any route
        self.td.tick(300)
        self.assertEqual(len(emb.applied), 1, emb.applied)

    def test_an_init_that_never_completes_still_applies_and_warns(self):
        """The attempt cap bounds the wait: a wedged init must still reach a
        verdict, never hang the orchestrator out to its timeout."""
        self.td.init_complete_at_s = None
        emb = self._driveToSetup(max_frames=20000)
        self.assertTrue(emb.applied, 'a wedged init must not block forever')
        self.assertTrue(any('_init_complete never set' in line
                            for line in self.td.logs), self.td.logs[-5:])

    def test_the_wizard_window_is_closed_before_the_setup_is_applied(self):
        emb = self._driveToSetup()
        self.assertIn('winclose', emb.op('window_wizard').pulses)


class TestHeadlessSetupAt12Fps(TestHeadlessSetupWaitsForInit):
    """Every proof above on a 12fps machine: the same wall-clock init lands
    on a completely different FRAME, which is why the frame count was never
    a deadline."""

    FPS = 12

    def test_init_lands_on_a_different_frame_than_at_60fps(self):
        self.td.init_complete_at_s = 2.0
        emb = self._driveToSetup()
        self.assertLess(emb.init_frame, 60)      # 2.0s * 12fps
        self.assertGreaterEqual(emb.applied[0][0], emb.init_frame)


# ===========================================================================
# 2. the deferred Envoyenable=False callback (the macOS CI bug)
# ===========================================================================

class TestDeferredEnvoyDisable(_Case):
    """init sets Envoyenable=False to stop an auto-start; TD defers that
    onValueChange and parexec can process it AFTER the headless setup
    enabled Envoy. Stop() then holds the whole run Disabled."""

    def setUp(self):
        super().setUp()
        # init writes Envoyenable=False at frame 10 and its callback is
        # deferred past _init_complete (frame 50) AND past the headless setup
        # (frame 55, the first attempt after init) -- landing at 70, when
        # parexec is live and Stop() bites.
        self.td.init_complete_at_s = self.td.secs(50)
        self.td.stale_disable_at_s = self.td.secs(10)
        self.td.stale_defer_frames = 60

    def test_the_stale_callback_really_lands_after_the_setup(self):
        """Without this the pair below proves nothing: the race has to
        happen in the fake before a fix can be credited for surviving it."""
        self.start()
        self.td.drive(until=lambda: bool(self.td.embody
                                         and self.td.embody.stop_frames),
                      max_frames=6000)
        emb = self.td.embody
        self.assertTrue(emb.applied, 'the setup never ran')
        disable = [w for w in emb.par_writes
                   if w[1] == 'Envoyenable' and not w[2]]
        self.assertTrue(disable, 'init never wrote Envoyenable=False')
        self.assertLess(disable[0][0], emb.applied[0][0],
                        'the disable must be WRITTEN before the setup')
        self.assertGreater(emb.stop_frames[0], emb.applied[0][0],
                           'its callback must LAND after the setup')
        self.assertEqual(str(emb.par.Envoystatus.eval()), 'Disabled')
        self.assertFalse(emb.par.Envoyenable.eval())

    def test_without_the_reassert_the_install_comes_up_disabled(self):
        """The bug, reproduced: drop the re-assertion and the fresh install
        reaches ready.flag with Envoy Disabled -- no leg is ever reached."""
        self.boot._reassert_envoy = lambda attempt=0: None
        ready = self.runToReady()
        self.assertEqual(ready['verdict'], 'FAIL')
        self.assertIn('Envoy not running', ready['problems'])
        self.assertEqual(ready['envoy_enabled'], 'False')

    def test_the_reassert_puts_envoy_back(self):
        ready = self.runToReady()
        self.assertEqual(ready['verdict'], 'PASS', ready['problems'])
        self.assertEqual(ready['envoy_enabled'], 'True')
        self.assertTrue(ready['envoy_status'].startswith('Running on port'))

    def test_a_second_late_callback_is_repaired_too(self):
        """The callback's frame is not ours to predict, so the re-assert
        checks several times."""
        self.start()
        self.td.drive(until=lambda: str(
            self.td.embody.par.Envoystatus.eval()).startswith('Running')
            if self.td.embody else False, max_frames=6000)
        emb = self.td.embody
        emb._stopEnvoy()                       # a second late callback
        self.td.drive(until=lambda: str(
            emb.par.Envoystatus.eval()).startswith('Running'), max_frames=6000)
        self.assertEqual(len(emb.stop_frames), 2)
        self.assertTrue(emb.par.Envoyenable.eval(),
                        'the second callback was never repaired')
        self.assertTrue(str(emb.par.Envoystatus.eval()).startswith('Running'))

    def test_the_reassert_is_scheduled_even_when_the_setup_raises(self):
        """A wizard that threw used to skip the race cover entirely."""
        self.td.wizard_setup_raises = True
        self.start()
        self.td.drive(until=lambda: bool(self.td.embody
                                         and self.td.embody.applied),
                      max_frames=6000)
        self.td.drive(until=lambda: bool(self.td.embody.stop_frames),
                      max_frames=6000)
        self.td.drive(until=lambda: self.flag('ready.flag') is not None,
                      max_frames=20000)
        self.assertTrue(self.td.embody.par.Envoyenable.eval(),
                        'the re-assert must be armed independently of the '
                        'wizard call succeeding')


class TestDeferredEnvoyDisableAt12Fps(TestDeferredEnvoyDisable):
    FPS = 12


# ===========================================================================
# 3. ready.flag
# ===========================================================================

class TestReadyFlag(_Case):

    def test_written_once_atomically_with_the_stamps(self):
        ready = self.runToReady()
        self.assertEqual(ready['verdict'], 'PASS', ready['problems'])
        self.assertEqual(ready['run_id'], 'windows-7')
        self.assertEqual(ready['platform'], 'win32')
        self.assertEqual(ready['tox'], 'Embody-v6.2.57.tox')
        self.assertEqual(ready['envoy_port'], 9871)
        self.assertEqual(ready['status'], 'Enabled')
        smoke.check_run_stamp(ready, 'windows-7')
        writes = self.wrote('ready.flag')
        self.assertEqual(len(writes), 1, 'ready.flag written %d times'
                         % len(writes))
        self.assertTrue(writes[0][1].endswith('ready.flag.tmp'),
                        'the flag must land via a sibling temp file')
        self.assertFalse(os.path.isfile(
            os.path.join(self.run_dir, 'ready.flag.tmp')))
        # Keep ticking: a second write would make a polling orchestrator
        # read two different verdicts for one run.
        self.td.tick(5000)
        self.assertEqual(len(self.wrote('ready.flag')), 1)

    def test_it_waits_out_a_pending_envoy_and_records_the_attempts(self):
        """The venv bootstrap is minutes, not frames: a flag written at 120
        frames caught Envoy mid-install and reported the opt-in par."""
        self.td.envoy_start_frames = 900
        ready = self.runToReady(max_frames=40000)
        self.assertEqual(ready['verdict'], 'PASS', ready['problems'])
        self.assertGreater(int(ready['settled_after_attempts']), 0)

    def test_no_flag_is_written_while_embody_is_still_initializing(self):
        """Status != Enabled means init is in flight; settling there
        reported a false FAIL (2026-07-29)."""
        self.td.init_complete_at_s = self.td.secs(400)
        self.start()
        self.td.tick(390)          # three ready polls have come and gone
        self.assertIsNone(self.flag('ready.flag'),
                          'the flag settled while Status was %r'
                          % self.td.embody.par.Status.eval())
        self.assertGreater(self.td.frame, 240)

    def test_an_envoy_that_never_started_fails_the_flag(self):
        self.td.envoy_start_fails = True
        ready = self.runToReady(max_frames=40000)
        self.assertEqual(ready['verdict'], 'FAIL')
        self.assertIn('Envoy not running', ready['problems'])
        self.assertIn('dependency install failed', ready['envoy_status'])

    def test_a_failed_startup_writes_features_as_all_skip(self):
        """Never a stale or absent features.flag: the orchestrator must see
        an explicit 'not reached'."""
        self.td.envoy_start_fails = True
        self.runToReady(max_frames=40000)
        feats = self.features()
        self.assertFalse(feats['terminal'] and feats['passed'])
        self.assertEqual(sorted(feats['failed']),
                         sorted(smoke.FEATURE_LEGS))
        for leg in smoke.FEATURE_LEGS:
            self.assertEqual(feats['legs'][leg]['verdict'], 'SKIP')

    def test_a_baked_off_clipboard_watcher_fails_the_flag(self):
        """v6.0.251 shipped it Off on every fresh install."""
        self.start()
        self.td.drive(until=lambda: bool(self.td.embody), max_frames=600)
        self.td.embody.par._vals['Clipboardautopaste'] = False
        self.td.drive(until=lambda: self.flag('ready.flag') is not None,
                      max_frames=40000)
        ready = self.ready()
        self.assertEqual(ready['verdict'], 'FAIL')
        self.assertIn('Clipboardautopaste', ready['problems'])

    def test_a_blank_updatestatus_fails_the_flag(self):
        self.start()
        self.td.drive(until=lambda: bool(self.td.embody), max_frames=600)
        self.td.embody.par._vals['Updatestatus'] = ''
        self.td.drive(until=lambda: self.flag('ready.flag') is not None,
                      max_frames=40000)
        self.assertIn('Updatestatus is BLANK', self.ready()['problems'])

    def test_the_problems_line_survives_the_parser(self):
        """problems holds '=' and ';'; parse_ready splits once."""
        self.td.envoy_start_fails = True
        self.td.init_complete_at_s = 2.0
        self.start()
        self.td.drive(until=lambda: bool(self.td.embody), max_frames=600)
        self.td.embody.par._vals['Updatestatus'] = ''
        self.td.drive(until=lambda: self.flag('ready.flag') is not None,
                      max_frames=40000)
        ready = self.ready()
        self.assertIn(';', ready['problems'])
        self.assertIn('Envoy not running', ready['problems'])
        self.assertIn('Updatestatus is BLANK', ready['problems'])


class TestReadyFlagAt12Fps(TestReadyFlag):
    FPS = 12


# ===========================================================================
# 4. features.flag
# ===========================================================================

class TestFeaturesFlag(_Case):

    def test_the_leg_order_is_the_orchestrators(self):
        self.assertEqual(tuple(self.boot.FEATURE_ORDER),
                         tuple(smoke.FEATURE_LEGS))

    def test_pending_while_outstanding_skip_only_when_final(self):
        pend = smoke.parse_features(self.boot._features_flag_text({}))
        self.assertFalse(pend['terminal'])
        for leg in smoke.FEATURE_LEGS:
            self.assertEqual(pend['legs'][leg]['verdict'], 'PENDING')
        final = smoke.parse_features(
            self.boot._features_flag_text({}, final=True))
        self.assertTrue(final['terminal'])
        self.assertFalse(final['passed'], 'SKIP must not count as a pass')
        self.assertEqual(sorted(final['failed']), sorted(smoke.FEATURE_LEGS))

    def test_every_leg_passes_on_a_healthy_install(self):
        ready, feats = self.runToEnd()
        self.assertEqual(ready['verdict'], 'PASS', ready['problems'])
        self.assertTrue(feats['terminal'])
        self.assertEqual(feats['failed'], [], feats['legs'])
        self.assertTrue(feats['passed'])
        for leg in smoke.FEATURE_LEGS:
            self.assertEqual(feats['legs'][leg]['verdict'], 'PASS',
                             feats['legs'][leg])

    def test_a_reader_polling_mid_phase_sees_pending_never_skip(self):
        """The file is rewritten after EVERY leg; a reader between two legs
        must keep waiting rather than count the rest as failed."""
        self.runToEnd()
        texts = [w[2] for w in self.wrote('features.flag')]
        self.assertGreater(len(texts), 2, 'one write per leg is the contract')
        mid = [smoke.parse_features(t) for t in texts[:-1]]
        self.assertTrue(any(not m['terminal'] for m in mid),
                        'no intermediate write was PENDING')
        for text in texts:
            self.assertNotIn('SKIP', text,
                             'a healthy run must never write SKIP')
        self.assertTrue(smoke.parse_features(texts[-1])['passed'])

    def test_a_failing_leg_is_one_line_and_does_not_stop_the_rest(self):
        self.td.checkpoint_fails = True
        ready, feats = self.runToEnd()
        self.assertEqual(feats['failed'], ['autosave_checkpoint'])
        self.assertEqual(feats['legs']['autosave_checkpoint']['verdict'],
                         'FAIL')
        self.assertIn('Checkpoint returned falsy',
                      feats['legs']['autosave_checkpoint']['detail'])
        self.assertEqual(feats['legs']['portable_export']['verdict'], 'PASS')
        text = self.flag('features.flag')
        self.assertEqual(len(text.strip().splitlines()),
                         len(smoke.FEATURE_LEGS),
                         'a detail with a newline would forge a leg verdict')

    def test_a_multiline_failure_cannot_forge_another_legs_verdict(self):
        """One line per leg: a newline inside a leg's exception would split
        the flag line, and the text after it parses as the next verdict."""
        self.td.portable_error = 'export failed\nviz_status=PASS|forged'
        ready, feats = self.runToEnd()
        text = self.flag('features.flag')
        self.assertEqual(len(text.strip().splitlines()),
                         len(smoke.FEATURE_LEGS), text)
        self.assertEqual(feats['failed'], ['portable_export'])
        self.assertNotIn('forged', feats['legs']['viz_status']['detail'])

    def test_the_convoy_leg_polls_to_a_terminal_verdict(self):
        ready, feats = self.runToEnd()
        self.assertEqual(feats['legs']['convoy']['verdict'], 'PASS')
        self.assertIn('Connected', feats['legs']['convoy']['detail'])

    def test_a_convoy_that_never_connects_times_out_as_a_failure(self):
        self.td.convoy_verdict = None
        ready, feats = self.runToEnd(max_frames=200000)
        self.assertEqual(feats['legs']['convoy']['verdict'], 'FAIL')
        self.assertIn('timed out', feats['legs']['convoy']['detail'])
        self.assertTrue(feats['terminal'])

    def test_a_refused_convoy_fails_without_waiting_out_the_poll(self):
        self.td.convoy_verdict = 'Error: host app install failed'
        ready, feats = self.runToEnd()
        self.assertEqual(feats['legs']['convoy']['verdict'], 'FAIL')
        self.assertLess(self.td.frame, 60 * self.boot.CONVOY_POLL_FRAMES)

    def test_the_project_is_saved_before_the_legs_run(self):
        """Every disk write Embody makes sits behind the on-disk gate: the
        legs are gated nonsense without a real save at the gate path."""
        self.runToEnd()
        self.assertEqual(
            self.td.project.saves,
            [os.path.join(self.run_dir, self.td.project.name)],
            'the save must name the gate path, never increment')

    def test_the_legs_wait_for_the_save_to_reach_disk(self):
        """Every disk write Embody makes sits behind the on-disk gate: legs
        run before the .toe lands are gated nonsense, not a verdict."""
        self.td.save_delay_frames = 150
        ready, feats = self.runToEnd()
        self.assertEqual(feats['legs']['tdn_roundtrip']['verdict'], 'PASS',
                         feats['legs']['tdn_roundtrip'])
        self.assertEqual(feats['failed'], [])

    def test_the_legs_wait_out_the_save_window(self):
        """Checkpoint correctly refuses inside the 120-frame window that
        outlives project.save()."""
        self.td.save_window_frames = 600
        ready, feats = self.runToEnd()
        self.assertEqual(feats['legs']['autosave_checkpoint']['verdict'],
                         'PASS', feats['legs']['autosave_checkpoint'])


class TestFeaturesFlagAt12Fps(TestFeaturesFlag):
    FPS = 12


# ===========================================================================
# 5. the seeded auto-responses: no dialog may ever block
# ===========================================================================

class TestSeededResponses(_Case):

    def test_no_dialog_in_a_whole_run_ever_reaches_ui_messagebox(self):
        self.runToEnd()
        emb = self.td.embody
        self.assertEqual(emb.blocked, [], 'a modal blocked an unattended run')
        self.assertEqual(self.td.ui.opened, [])
        answered = dict(emb.dialogs)
        for title in ('Embody', 'Embody - AI Coding Assistant Integration',
                      'Envoy -- Git Repository Recommended'):
            self.assertIn(title, answered, emb.dialogs)
            self.assertEqual(answered[title],
                             self.boot.SMOKE_RESPONSES[title])

    def test_the_store_outlives_its_last_consumed_key(self):
        """An EMPTY store is what lets an unanticipated dialog fall through:
        the sentinel names no dialog and is never consumed."""
        self.runToEnd()
        emb = self.td.embody
        left = emb.fetch('_smoke_test_responses', None, search=False)
        self.assertIsNotNone(left, 'the response store self-destructed')
        self.assertIn(self.boot.SMOKE_SENTINEL, left)
        emb.messageBox('Something Nobody Anticipated')
        self.assertEqual(emb.blocked, [])
        self.assertEqual(emb.dialogs[-1], ('Something Nobody Anticipated', -1))

    def test_a_dropped_seed_would_block(self):
        """The fake is only evidence if a MISSING seed really blocks."""
        self.start()
        self.td.drive(until=lambda: bool(self.td.embody
                                         and self.td.embody.fetch(
                                             '_smoke_test_responses')),
                      max_frames=2000)
        emb = self.td.embody
        emb.unstore('_smoke_test_responses')
        emb.messageBox('Embody - AI Coding Assistant Integration')
        self.assertEqual(emb.blocked,
                         ['Embody - AI Coding Assistant Integration'])

    def test_the_seeding_happens_before_the_first_dialog(self):
        self.start()
        first = min(self.td.dialogs_at)
        self.td.drive(until=lambda: bool(self.td.embody
                                         and self.td.embody.fetch(
                                             '_smoke_test_responses')),
                      max_frames=2000)
        self.assertLess(self.td.frame, first + 1,
                        'responses were seeded after a dialog could fire')

    def test_convoy_titles_are_seeded_before_convoy_is_enabled(self):
        self.runToEnd()
        seed = self.td.embody.convoy_seed
        self.assertIsNotNone(seed, 'Convoyenable was never set')
        for title in self.boot.SMOKE_CONVOY_RESPONSES:
            self.assertIn(title, seed)
        self.assertIn(self.boot.SMOKE_SENTINEL, seed)

    def test_the_seeded_set_is_exactly_the_init_dialogs(self):
        """A title the product can raise during init and this set does not
        hold is a blocking modal in an unattended run."""
        self.assertEqual(
            set(self.boot.SMOKE_RESPONSES),
            {'Embody', 'Embody - AI Coding Assistant Integration',
             'Envoy -- Git Repository Recommended', self.boot.SMOKE_SENTINEL})


# ===========================================================================
# 6. the abort path
# ===========================================================================

class TestAbortPath(_Case):

    def _assertAborted(self, reason):
        ready = self.ready()
        self.assertIsNotNone(ready, 'no flag: the orchestrator would burn '
                                    'its whole ready timeout')
        self.assertEqual(ready['verdict'], 'FAIL')
        self.assertIn(reason, ready['problems'])
        self.assertEqual(ready['run_id'], 'windows-7')
        self.assertEqual(ready['platform'], 'win32')
        feats = self.features()
        self.assertTrue(feats['terminal'])
        self.assertFalse(feats['passed'])
        self.assertEqual(sorted(feats['failed']), sorted(smoke.FEATURE_LEGS))
        self.assertTrue(self.wrote('ready.flag')[0][1].endswith('.tmp'))

    def test_a_missing_release_tox_aborts_instead_of_hanging(self):
        os.remove(self.tox)
        self._sidecar(tox_path=os.path.join(self.repo, 'release', 'gone.tox'))
        self.start()
        self._assertAborted('release .tox not found')
        self.assertEqual(self.td.queue, [],
                         'nothing may still be scheduled after an abort')

    def test_an_unreadable_manifest_aborts(self):
        """A manifest that is present but unusable must never fall back to a
        DIFFERENT build -- it aborts."""
        os.remove(self.tox)
        with open(os.path.join(self.repo, 'release', 'embody-release.json'),
                  'w', encoding='utf-8') as f:
            f.write('{not json')
        self._sidecar(tox_path='')
        self.start()
        self._assertAborted('release .tox not found')

    def test_a_tox_that_vanished_between_select_and_load_aborts(self):
        self.start()
        os.remove(self.tox)
        self.td.tick(5)
        self._assertAborted('.tox not found')

    def test_a_loadtox_that_raises_aborts_with_the_exception(self):
        self.td.loadtox_raises = True
        self.start()
        self.td.tick(5)
        self._assertAborted('loadTox raised RuntimeError')

    def test_a_loadtox_that_returns_none_aborts(self):
        self.td.loadtox_none = True
        self.start()
        self.td.tick(5)
        self._assertAborted('loadTox returned None')

    def test_the_abort_flag_carries_every_key_the_parser_reads(self):
        os.remove(self.tox)
        self._sidecar(tox_path=os.path.join(self.repo, 'release', 'gone.tox'))
        self.start()
        ready = self.ready()
        for key in ('verdict', 'problems', 'version', 'status',
                    'envoy_enabled', 'envoy_status', 'run_id', 'platform',
                    'tox'):
            self.assertIn(key, ready)
        self.assertEqual(ready['status'], 'NOT_LOADED')

    def test_a_stale_mid_run_save_is_called_out(self):
        """Opening smoke_template.N.toe instead of the pristine template
        boots mid-run state and skips the whole headless setup."""
        self.td.me.store('headless_setup_done', True)
        self.start()
        self.assertTrue(any('NOT a virgin install' in line
                            for line in self.td.logs), self.td.logs)
        self.assertFalse(self.td.me.fetch('headless_setup_done', False),
                         'the per-run reset must clear leaked state')


class TestDisabledIsNotAVerdictTooEarly(TestHeadlessSetupWaitsForInit):
    """'Disabled' was terminal, so a poll landing before the smoke had
    enabled Envoy -- or inside the window where a stale init callback had
    just Stop()ped it -- settled FAIL on a healthy install. That is what
    the macOS runner reported twice: 'ready FAIL settled after 7 polls
    Disabled' (2026-09-20)."""

    def _settled(self, envoy_status, setup_done, reassert_done):
        emb = self._driveToSetup()      # the Embody COMP exists after this
        emb.par._vals['Envoystatus'] = envoy_status
        emb.par._vals['Status'] = 'Enabled'
        self.td.me.store('headless_setup_done', setup_done)
        self.td.me.store('envoy_reassert_done', reassert_done)
        return self.boot._envoy_settled(emb)

    def test_disabled_before_the_setup_is_not_settled(self):
        settled, status = self._settled('Disabled', False, False)
        self.assertFalse(settled, 'settled before the smoke enabled Envoy')
        self.assertEqual(status, 'Disabled')

    def test_disabled_inside_the_reassert_window_is_not_settled(self):
        settled, _ = self._settled('Disabled', True, False)
        self.assertFalse(settled, 'settled while the re-assert could repair')

    def test_disabled_after_both_is_the_real_answer(self):
        settled, _ = self._settled('Disabled', True, True)
        self.assertTrue(settled, 'a genuinely disabled Envoy must be reported')

    def test_a_running_envoy_still_settles_immediately(self):
        settled, _ = self._settled('Running on port 9871', False, False)
        self.assertTrue(settled)
