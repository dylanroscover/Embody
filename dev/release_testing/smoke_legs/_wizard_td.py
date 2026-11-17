"""
TD side of the `wizard` smoke leg: staged as smoke_bootstrap.py in a run dir
of its own, so the template's execute DAT runs THIS instead of the smoke.

Mimics a first-time drag-in: the project already holds two COMPs carrying
the External .tox link TD writes on drag-in, then the release .tox lands with
the same link on itself. Clicks through the REAL setup wizard (Unlink; Envoy,
git, externalize and Convoy off) and writes wizard_check.json beside the .toe
for smoke_legs/wizard.py to judge. Deletes nothing.
"""
import json
import os

FLAG = 'wizard_check.json'
DIALOG = 'Dropped .tox Expression Detected'
SEED = 5
EXPR = "me.parent().fileFolder + '/' + '%s.tox'"
USERS = ('DROSTE', 'TDTodo')
# frame caps, not seconds: init is frame-scheduled, so a slow machine
# reaches the wizard in about the same number of frames
WATCH_POLLS = 1800      # x2 frames
DRIVE_CLICKS = 60       # the spine is 7-8 steps
CHOICES = {'toxdrop': 'opt_clean', 'externalize': 'opt_skip',
           'assistant': 'opt_none', 'convoy': 'opt_disable',
           'git': 'opt_gitskip'}
R = {'log': []}


def _log(m):
    R['log'].append('%d %s' % (absTime.frame, m))


def _left(emb):
    resp = emb.fetch('_smoke_test_responses', None, search=False) or {}
    return len(resp.get(DIALOG, []))


def _write():
    path = os.path.join(project.folder, FLAG)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(R, f, indent=1, default=str)
    os.replace(path + '.tmp', path)


def _fail(msg):
    import traceback
    R['error'] = '%s\n%s' % (msg, traceback.format_exc())
    _write()


def _sawDialog(emb, win):
    if _left(emb) < SEED and 'dialog_frame' not in R:
        R['dialog_frame'] = absTime.frame
        _log('dropped-.tox DIALOG fired (wizard open: %s)' % win.isOpen)


def onStart():
    run("args[0]()", _drop, delayFrames=1)


def _drop():
    try:
        with open(os.path.join(project.folder, 'smoke_run.json'),
                  encoding='utf-8') as f:
            tox = json.load(f)['tox_path']
        if op('/Embody'):
            op('/Embody').destroy()
        for i, n in enumerate(USERS):
            c = op('/' + n) or op('/').create(baseCOMP, n)
            c.nodeX, c.nodeY = 0, -400 * (i + 2)
            c.par.externaltox.expr = EXPR % n
        emb = op('/').loadTox(tox)
        # what a real drag-in writes into the dropped COMP
        emb.par.externaltox.expr = EXPR % os.path.splitext(
            os.path.basename(tox))[0]
        emb.store('_smoke_test_responses', {
            DIALOG: [1] * SEED, '__smoke_sentinel__': 0, 'Embody': 0,
            'Embody - AI Coding Assistant Integration': 0,
            'Envoy -- Git Repository Recommended': 3})
        me.store('emb', emb.path)
        R['tox'] = os.path.basename(tox)
        _log('dropped %s' % R['tox'])
        run("args[0](args[1])", _watch, 0, delayFrames=2)
    except Exception:
        _fail('drop failed')


def _watch(n):
    try:
        emb = op(me.fetch('emb'))
        win = emb.op('window_wizard')
        _sawDialog(emb, win)
        if win.isOpen:
            _log('wizard open')
            run("args[0](args[1])", _drive, 0, delayFrames=20)
            return
        if n > WATCH_POLLS:
            R['error'] = 'wizard never opened'
            _write()
            return
        run("args[0](args[1])", _watch, n + 1, delayFrames=2)
    except Exception:
        _fail('watch failed')


def _shoot(w):
    """Best-effort picture of the step, for a human; never asserted."""
    try:
        v = op('/wizard_shot') or op('/').create(opviewerTOP, 'wizard_shot')
        v.nodeX, v.nodeY = 600, -400
        v.par.opviewer = v.relativePath(w)
        v.par.outputresolution = 'custom'
        v.par.resolutionw, v.par.resolutionh = 1280, 720
        v.par.outputaspect = 'resolution'
    except Exception as e:
        _log('screenshot setup failed: %s' % e)


def _drive(k):
    try:
        emb = op(me.fetch('emb'))
        w = emb.op('wizard')
        m = w.op('logic').module
        win = emb.op('window_wizard')
        _sawDialog(emb, win)
        if not win.isOpen:
            _log('wizard closed')
            run("args[0]()", _verdict, delayFrames=30)
            return
        cur = w.fetch('step_id', 'mode')
        if cur == 'toxdrop' and 'toxdrop_hint' not in R:
            R['toxdrop_hint'] = w.op('hint').par.text.eval()
            R['toxdrop_preselected'] = m._chosen('toxdrop')
            _shoot(w)
            run("args[0](args[1])", _drive, k, delayFrames=10)
            return
        if cur == 'toxdrop' and op('/wizard_shot'):
            try:
                op('/wizard_shot').save(
                    os.path.join(project.folder, 'wizard_toxdrop.png'))
            except Exception as e:
                _log('screenshot save failed: %s' % e)
        R.setdefault('steps', [])
        if not R['steps'] or R['steps'][-1] != cur:
            R['steps'].append(cur)
        if cur in CHOICES:
            m.click(CHOICES[cur])
        if cur == 'summary':
            R['recap'] = w.op('hint').par.text.eval()
        m.click('btn_next')
        if k > DRIVE_CLICKS:
            R['error'] = 'wizard did not finish (stuck on %s)' % cur
            _write()
            return
        run("args[0](args[1])", _drive, k + 1, delayFrames=8)
    except Exception:
        _fail('drive failed')


def _verdict():
    try:
        emb = op(me.fetch('emb'))
        _sawDialog(emb, emb.op('window_wizard'))
        R['pref_after'] = emb.par.Toxdropexpr.eval()
        R['user_links_left'] = [
            n for n in USERS
            if 'fileFolder' in (op('/' + n).par.externaltox.expr or '')]
        R['embody_link_left'] = 'fileFolder' in (
            emb.par.externaltox.expr or '')
        R['version'] = emb.par.Version.eval()
        _log('verdict written')
        _write()
    except Exception:
        _fail('verdict failed')


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
    return


def onProjectPostSave():
    return
