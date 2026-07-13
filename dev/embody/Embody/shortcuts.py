"""Editable keyboard shortcuts: normalization, dispatch, validation, recording.

Module DAT (issue #50). Bindings live as Str pars on the Embody COMP's
Shortcuts page; keyboardin_callbacks.py dispatches through buildDispatch()
and parexec.py routes the Record pulses and value edits here. No TD objects
are touched at import time.

Combo format: lowercase, '+'-joined, modifiers first, one trigger key last.
'ctrl' and 'cmd' are DISTINCT modifiers naming physical keys: macOS
keyboards have both (matched exactly there), PC keyboards have only Ctrl,
so a Mac-authored 'cmd+...' binding folds to 'ctrl+...' at match/display
time on Windows/Linux (the stored value is never rewritten, so it
round-trips between platforms intact). Factory defaults use the platform's
primary modifier: 'cmd+...' on macOS, 'ctrl+...' elsewhere. A combo may be
assigned to only ONE action (duplicates are blocked, not just warned). An
empty value disables the shortcut. Left/right variants fold together.
"""

import sys as _sys

_IS_MAC = _sys.platform == 'darwin'

# Modifier used by the FACTORY DEFAULTS on this platform (the idiomatic
# app-shortcut modifier). Custom bindings can use either key on macOS.
PRIMARY_TOKEN = 'cmd' if _IS_MAC else 'ctrl'

# Key names the Keyboard In DAT delivers for modifier presses (onKey 'key').
MODIFIER_KEYS = {
    'lctrl', 'rctrl', 'lalt', 'ralt', 'lshift', 'rshift',
    'lcmd', 'rcmd', 'ctrl', 'alt', 'shift', 'cmd',
    'capslock', 'numlock',
}

# Modifier tokens accepted in typed combos -> canonical form. ctrl and cmd
# stay distinct (they are different physical keys on macOS).
_MODIFIER_ALIASES = {
    'ctrl': 'ctrl', 'control': 'ctrl', 'lctrl': 'ctrl', 'rctrl': 'ctrl',
    'cmd': 'cmd', 'command': 'cmd', 'lcmd': 'cmd', 'rcmd': 'cmd',
    'alt': 'alt', 'option': 'alt', 'opt': 'alt', 'lalt': 'alt', 'ralt': 'alt',
    'shift': 'shift', 'lshift': 'shift', 'rshift': 'shift',
}

_MODIFIER_ORDER = ('ctrl', 'cmd', 'alt', 'shift')

# Named (non-character) trigger keys accepted in combos, matching the
# Keyboard In DAT's key names. Esc is deliberately absent: it is the
# recorder's cancel key and TD owns it.
_NAMED_KEYS = {
    'tab', 'backtab', 'enter', 'space', 'linefeed', 'backspace', 'period',
    'insert', 'home', 'pageup', 'delete', 'end', 'pagedown',
    'left', 'right', 'up', 'down',
    'printscreen', 'scrolllock', 'pause', 'break',
}

# The remappable actions: par name, label, factory default (built on the
# platform's primary modifier -- Cmd on macOS, Ctrl elsewhere).
ACTIONS = (
    ('Shortcutmanager', 'Open Manager', f'{PRIMARY_TOKEN}+shift+o'),
    ('Shortcutupdateall', 'Update All Externalizations', f'{PRIMARY_TOKEN}+shift+u'),
    ('Shortcutupdatecomp', 'Update Current COMP', f'{PRIMARY_TOKEN}+alt+u'),
    ('Shortcutrefresh', 'Refresh Tracking', f'{PRIMARY_TOKEN}+shift+r'),
    ('Shortcutexportproject', 'Export Project to TDN', f'{PRIMARY_TOKEN}+shift+e'),
    ('Shortcutexportcomp', 'Export Current COMP to TDN', f'{PRIMARY_TOKEN}+alt+e'),
    ('Shortcutcopytdn', 'Copy Selected COMP as TDN', f'{PRIMARY_TOKEN}+shift+c'),
)
SHORTCUT_PARS = tuple(a[0] for a in ACTIONS)

# Record pulse par -> the Str par it records into (Recordmanager -> Shortcutmanager).
RECORD_PARS = {'Record' + p[len('Shortcut'):]: p for p in SHORTCUT_PARS}

# Tagger menu: the entries are PHYSICAL keys, so the choices differ per
# platform -- macOS offers both Ctrl and Cmd (they are distinct keys there),
# Windows/Linux offers Ctrl only (no Cmd key exists). The lists are served
# live through the par's menuSource, so the same saved .toe presents the
# right choices on whichever platform opens it. A stored value is NEVER
# rewritten for the platform: a Mac-authored 'lcmd' opened on a PC folds to
# Ctrl at match/display time only, so moving the project back to the Mac
# restores the exact original binding.
if _IS_MAC:
    # No right-Ctrl entry: Apple keyboards have a single (left) Control key.
    TAGGER_MENU_NAMES = ('off', 'lctrl', 'lcmd', 'rcmd',
                         'lalt', 'ralt', 'lshift', 'rshift')
    TAGGER_MENU_LABELS = ('Off', 'Double Left Ctrl',
                          'Double Left Cmd', 'Double Right Cmd',
                          'Double Left Alt', 'Double Right Alt',
                          'Double Left Shift', 'Double Right Shift')
else:
    TAGGER_MENU_NAMES = ('off', 'lctrl', 'rctrl', 'lalt', 'ralt',
                         'lshift', 'rshift')
    TAGGER_MENU_LABELS = ('Off', 'Double Left Ctrl', 'Double Right Ctrl',
                          'Double Left Alt', 'Double Right Alt',
                          'Double Left Shift', 'Double Right Shift')
TAGGER_DEFAULT = 'lctrl'

# Physical-key fold applied at match/display time ONLY (stored values are
# never rewritten, so they round-trip between platforms intact). On PC
# there is no Cmd key, so Mac-authored Cmd values act as Ctrl. On Mac
# keyboards there is no right Ctrl key, so PC-authored 'rctrl' acts as
# (left) Ctrl -- and a right-Ctrl press from an extended third-party
# keyboard folds the same way, so it works too.
_TAGGER_FOLD = ({'rctrl': 'lctrl'} if _IS_MAC
                else {'lcmd': 'lctrl', 'rcmd': 'rctrl'})


class _MenuObject:
    """Duck-typed menu source: TD reads .menuNames/.menuLabels from it."""

    def __init__(self, names, labels):
        self.menuNames = list(names)
        self.menuLabels = list(labels)


def taggerMenu():
    """Live menuSource for the Shortcuttagger par (platform-aware choices)."""
    return _MenuObject(TAGGER_MENU_NAMES, TAGGER_MENU_LABELS)


def taggerKeyMatches(stored, key):
    """True when a physical modifier keypress matches the stored tap key.

    Ctrl and Cmd are separate menu choices on macOS and match exactly;
    keys the current platform's keyboards lack (Cmd on PC, right Ctrl on
    Mac) fold onto their closest existing key on both sides of the compare,
    so foreign-authored bindings degrade gracefully.
    """
    stored, key = str(stored), str(key)
    if stored == 'off':
        return False
    return _TAGGER_FOLD.get(key, key) == _TAGGER_FOLD.get(stored, stored)


def taggerDisplayKey(stored):
    """The tap key as it behaves on THIS platform ('lcmd' -> 'lctrl' on PC,
    'rctrl' -> 'lctrl' on Mac)."""
    stored = str(stored)
    return _TAGGER_FOLD.get(stored, stored)

# Factory default per action par, for validation fallbacks.
DEFAULTS = {p: d for p, _l, d in ACTIONS}

_REC_KEY = '_shortcut_rec'
_REC_GEN_KEY = '_shortcut_rec_gen'
_REC_TIMEOUT_SECONDS = 10.0


def _canonicalTrigger(token):
    """Canonical trigger-key name, or None if the token is not bindable."""
    if len(token) == 1 and token.isprintable() and not token.isspace():
        return token
    if token in _NAMED_KEYS:
        return token
    if 2 <= len(token) <= 3 and token[0] == 'f' and token[1:].isdigit() \
            and 1 <= int(token[1:]) <= 12:
        return 'F' + token[1:]
    return None


def normalize(text):
    """Canonical form of a combo ('Ctrl + Shift + O' -> 'ctrl+shift+o').

    Returns '' for an empty value (shortcut disabled), or None if the text
    is not a valid combo (no trigger key, two trigger keys, unknown key).
    """
    raw = str(text or '').strip().lower()
    if not raw:
        return ''
    import re
    tokens = [t for t in re.split(r'[\s\+\-\.]+', raw) if t]
    mods, trigger = [], None
    for t in tokens:
        if t in _MODIFIER_ALIASES:
            m = _MODIFIER_ALIASES[t]
            if m not in mods:
                mods.append(m)
        elif trigger is None:
            trigger = t
        else:
            return None
    if trigger is None:
        return None
    trigger = _canonicalTrigger(trigger)
    if trigger is None:
        return None
    ordered = [m for m in _MODIFIER_ORDER if m in mods]
    return '+'.join(ordered + [trigger])


def _ctrlFold(combo):
    """Fold the 'cmd' token onto 'ctrl' (dedup preserving order).

    Used for matching on platforms without a Cmd key, and for comparing
    against TD's reserved table (whose rows are written ctrl-form but mean
    Cmd on macOS).
    """
    combo = str(combo or '').strip()
    if not combo:
        return ''
    parts = []
    for p in combo.split('+'):
        p = 'ctrl' if p == 'cmd' else p
        if p not in parts:
            parts.append(p)
    return '+'.join(parts)


def matchForm(combo):
    """The combo as it BEHAVES on this platform.

    macOS: identity -- Ctrl and Cmd are distinct physical keys, so
    'ctrl+shift+o' and 'cmd+shift+o' are different bindings. PC: no Cmd key
    exists, so 'cmd' folds to 'ctrl' and Mac-authored bindings still fire.
    """
    combo = str(combo or '').strip()
    return combo if _IS_MAC else _ctrlFold(combo)


def comboFromEvent(key, ctrl, alt, shift, cmd):
    """Normalized combo for a Keyboard In DAT key event.

    Ctrl and Cmd are reported as the distinct physical modifiers they are.
    The trigger is canonicalized through the same rules as typed input so a
    RECORDED binding and a TYPED binding land in one canonical space (F-key
    casing especially) and buildDispatch lookups always match.
    """
    k = str(key)
    canon = _canonicalTrigger(k.lower())
    if canon is not None:
        k = canon
    mods = []
    if ctrl:
        mods.append('ctrl')
    if cmd:
        mods.append('cmd')
    if alt:
        mods.append('alt')
    if shift:
        mods.append('shift')
    return '+'.join(mods + [k])


def _isMac():
    return _IS_MAC


def display(combo):
    """Human-readable form of a combo as it BEHAVES on this platform.

    macOS renders the stored tokens verbatim ('ctrl+shift+o' ->
    'Ctrl+Shift+O', 'cmd+shift+o' -> 'Cmd+Shift+O'); a PC folds Mac-authored
    'cmd' to the Ctrl it actually fires on. '' -> 'unassigned'.
    """
    combo = matchForm(combo)
    if not combo:
        return 'unassigned'
    return '+'.join(
        p if (p.startswith('F') and p[1:].isdigit()) else p.capitalize()
        for p in combo.split('+'))


def buildDispatch(comp):
    """Map each assigned combo -> its action par name (first assignment wins).

    Keys are in matchForm space, so lookups must fold the event combo
    through matchForm() too -- stored 'cmd+...' and 'ctrl+...' spellings
    both dispatch on every platform.
    """
    table = {}
    for par_name, _label, _default in ACTIONS:
        combo = str(comp.par[par_name].eval()).strip()
        if combo:
            table.setdefault(matchForm(combo), par_name)
    return table


def actionForEvent(comp, key, ctrl, alt, shift, cmd):
    """The action par name a key event dispatches to, or None."""
    return buildDispatch(comp).get(
        matchForm(comboFromEvent(key, ctrl, alt, shift, cmd)))


def _comboFromTdKey(key):
    """Normalized combo for a TouchShortcuts.txt key cell ('ctrl.shift.s').

    Dot-separated, modifiers first. Parsed directly (not via normalize())
    because the file binds keys our typed-combo grammar treats as
    separators ('+', '-', '.').
    """
    key = key.strip()
    if not key or key == '000':
        return None
    parts = ['.'] if key == '.' else [p for p in key.split('.') if p]
    if not parts:
        return None
    mods, trigger = [], None
    for t in parts:
        low = t.lower()
        if low in _MODIFIER_ALIASES:
            m = _MODIFIER_ALIASES[low]
            if m not in mods:
                mods.append(m)
        elif trigger is None:
            trigger = low
        else:
            return None
    if trigger is None:
        return None
    canon = _canonicalTrigger(trigger)
    trigger = canon if canon is not None else trigger
    ordered = [m for m in _MODIFIER_ORDER if m in mods]
    return '+'.join(ordered + [trigger])


def reservedTdCombos():
    """Combos TouchDesigner itself owns, from the effective TouchShortcuts.txt.

    The factory table (app.configFolder) lists every built-in; its command
    column is optional (app-level rows leave it blank), so every factory row
    with a key counts. The user override table (app.preferencesFolder)
    remaps a built-in when it gives a key, and DISABLES one when its command
    column is blanked.
    """
    import os
    reserved = {}
    for folder, is_override in ((app.configFolder, False),
                                (app.preferencesFolder, True)):
        path = folder + '/TouchShortcuts.txt'
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            cells = line.split('\t')
            label = cells[0].strip() if cells else ''
            if not label or label == 'label':
                continue
            key = cells[1].strip() if len(cells) > 1 else ''
            command = cells[2].strip() if len(cells) > 2 else ''
            if is_override and not command:
                reserved.pop(label, None)
                continue
            combo = _comboFromTdKey(key)
            if combo:
                reserved[label] = combo
    return set(reserved.values())


def duplicateOf(comp, target_par, combo):
    """Par name of another action already holding this combo, or None.

    Compared in matchForm (behavior) space. Duplicates are BLOCKED -- a
    combo may drive only one action.
    """
    mf = matchForm(combo)
    if not mf:
        return None
    for par_name, _label, _default in ACTIONS:
        if par_name == target_par:
            continue
        if matchForm(str(comp.par[par_name].eval()).strip()) == mf:
            return par_name
    return None


def actionLabel(par_name):
    """Human label for an action par name."""
    return next((l for p, l, _d in ACTIONS if p == par_name), par_name)


def validate(comp, target_par, combo):
    """Warning strings for a combo about to be assigned to target_par.

    Covers TD built-ins only (duplicates are blocked outright via
    duplicateOf, not warned). The reserved compare always ctrl-folds both
    sides: TD's table rows are written ctrl-form but mean Cmd on macOS.
    """
    warnings = []
    if combo and _ctrlFold(combo) in reservedTdCombos():
        warnings.append(
            f"{display(combo)} is a TouchDesigner built-in shortcut -- "
            "TD's own action will fire too (Embody cannot suppress it)")
    return warnings


# One-line descriptions for the in-app help panel.
HELP_DESCRIPTIONS = {
    'Shortcutmanager': 'Open the Manager (live externalizations list).',
    'Shortcutupdateall': 'Update all externalizations.',
    'Shortcutupdatecomp': 'Update only the COMP you are currently inside.',
    'Shortcutrefresh': 'Refresh tracking state.',
    'Shortcutexportproject': 'Export the whole project to .tdn.',
    'Shortcutexportcomp': 'Export just the current network to .tdn.',
    'Shortcutcopytdn': 'Copy the selected COMP as portable TDN.',
}


def helpBlock(comp, width=62):
    """Plain-text shortcut list for the help panel's {{SHORTCUTS}} token."""
    lines = []
    tap = str(comp.par.Shortcuttagger.eval())
    if tap != 'off':
        d = taggerDisplayKey(tap)
        lines.append((f'{d}-{d}',
                      'Tag the operator under the cursor (or, if it is '
                      'already tagged, open its Actions menu).'))
    for par_name, label, _default in ACTIONS:
        combo = str(comp.par[par_name].eval()).strip()
        if combo:
            lines.append((display(combo),
                          HELP_DESCRIPTIONS.get(par_name, label + '.')))
    if not lines:
        return '(no keyboard shortcuts assigned)'
    import textwrap
    pad = max(len(c) for c, _ in lines) + 2
    out = []
    for combo, desc in lines:
        wrapped = textwrap.wrap(desc, max(20, width - 2 - pad))
        out.append(f'- {combo.ljust(pad)}{wrapped[0]}')
        out.extend(' ' * (2 + pad) + w for w in wrapped[1:])
    return '\n'.join(out)


# -- Recording -----------------------------------------------------------
# Armed by a Record pulse; the first non-modifier keydown commits, Esc
# cancels, and a deadline auto-disarms an abandoned recording (a parameter
# pulse has no focus to lose, so the timeout substitutes for the click-away
# cancel that desktop hotkey recorders rely on).

def recordingActive(comp):
    import time
    rec = comp.fetch(_REC_KEY, None, search=False)
    if not rec:
        return None
    # Wall clock, not absTime: absTime resets on restart, so an armed state
    # baked into a saved .toe would otherwise resurrect as live.
    if time.time() > rec.get('deadline', 0):
        comp.unstore(_REC_KEY)
        return None
    return rec


def arm(comp, par_name):
    """Arm the recorder for one Shortcut par (called from a Record pulse)."""
    labels = {p: l for p, l, _d in ACTIONS}
    label = labels.get(par_name)
    if label is None:
        return
    # Refuse to arm when the recorder can never receive a key event --
    # otherwise it just times out and the user has no idea why.
    if comp.par.Performmode.eval():
        ui.status = 'Embody: cannot record shortcuts in Perform Mode'
        return
    if not comp.par.Enablekeyboardshortcuts.eval():
        ui.status = ('Embody: keyboard shortcuts are disabled -- turn on '
                     "'Enable Keyboard Shortcuts' before recording")
        return
    import time
    # The generation counter lives in its OWN key and only ever increments:
    # deriving it from the recording entry (which commit/cancel/expiry
    # unstore) would restart it at 1, letting a previous pulse's pending
    # _expire timer kill the NEXT recording.
    gen = comp.fetch(_REC_GEN_KEY, 0, search=False) + 1
    comp.store(_REC_GEN_KEY, gen)
    comp.store(_REC_KEY, {
        'target': par_name, 'label': label, 'gen': gen,
        'deadline': time.time() + _REC_TIMEOUT_SECONDS,
    })
    ui.status = (f"Embody: recording shortcut for '{label}' -- press a key "
                 'combo (Esc cancels)')
    comp.Log(f"Recording shortcut for '{label}' -- press a key combo; "
             f'Esc cancels, times out in {int(_REC_TIMEOUT_SECONDS)}s', 'INFO')
    run(_expire, comp.path, gen,
        delayMilliSeconds=int(_REC_TIMEOUT_SECONDS * 1000) + 500)


def _expire(comp_path, gen):
    comp = op(comp_path)
    if comp is None:
        return
    rec = comp.fetch(_REC_KEY, None, search=False)
    if rec and rec.get('gen') == gen:
        comp.unstore(_REC_KEY)
        ui.status = (f"Embody: shortcut recording for '{rec['label']}' "
                     'timed out -- binding unchanged')


def handleRecordingKey(comp, key, ctrl, alt, shift, cmd, state):
    """Consume a key event while a recording is armed.

    Returns True if the event belonged to the recorder (caller must not
    dispatch it), False when no recording is active.
    """
    rec = recordingActive(comp)
    if rec is None:
        return False
    if not state:
        return True
    if key == 'esc':
        comp.unstore(_REC_KEY)
        ui.status = 'Embody: shortcut recording cancelled'
        return True
    if key in MODIFIER_KEYS:
        mods = [m for m, on in (('ctrl', ctrl), ('cmd', cmd), ('alt', alt),
                                ('shift', shift)) if on]
        if mods:
            ui.status = (f"Embody: recording '{rec['label']}' -- "
                         + '+'.join(m.capitalize() for m in mods) + '+...')
        return True
    combo = comboFromEvent(key, ctrl, alt, shift, cmd)
    # Refuse keys the typed-combo grammar cannot round-trip ('-', '.', '+'
    # are separators; unknown key names) -- committing one would announce
    # success and then silently revert on the next-frame normalization
    # pass. Stay armed so the user can try another key.
    if normalize(combo) != combo:
        ui.status = (f"Embody: '{key}' cannot be bound -- try another key "
                     '(Esc cancels)')
        return True
    # Refuse combos already assigned to another action -- one combo drives
    # exactly one action. Alert with a modal (a status-bar line is easy to
    # miss and the user is left wondering why nothing was recorded), then
    # re-arm with a fresh deadline since the modal ate recording time. Goes
    # through Embody's _messageBox so test runs and the save window
    # auto-respond instead of freezing on a modal.
    dup = duplicateOf(comp, rec['target'], combo)
    if dup is not None:
        ui.status = (f"Embody: {display(combo)} is already assigned to "
                     f"'{actionLabel(dup)}' -- press another key (Esc cancels)")
        comp.ext.Embody._messageBox(
            'Embody',
            f'{display(combo)} is already assigned to '
            f'"{actionLabel(dup)}".\n\n'
            f'Each shortcut can drive only one action. Still recording '
            f'"{rec["label"]}" -- press a different key combo, or Esc '
            'to cancel.',
            buttons=['Ok'])
        arm(comp, rec['target'])
        return True
    comp.unstore(_REC_KEY)
    warnings = validate(comp, rec['target'], combo)
    comp.par[rec['target']] = combo
    msg = f"Embody: '{rec['label']}' shortcut set to {display(combo)}"
    if warnings:
        msg += ' -- ' + '; '.join(warnings)
        for w in warnings:
            comp.Log(f"Shortcut warning ({rec['label']}): {w}", 'WARNING')
    ui.status = msg
    comp.Log(f"Shortcut for '{rec['label']}' set to {display(combo)}",
             'SUCCESS')
    return True


def taggerTapDisplay(comp):
    """Help-panel phrase for the tagger double-tap, platform-idiomatic."""
    tap = str(comp.par.Shortcuttagger.eval())
    if tap == 'off':
        return 'the tagger key (currently off)'
    d = taggerDisplayKey(tap)
    return f'{d} twice ({d}-{d})'


def resetDefaults(comp):
    """Restore every shortcut (and the tagger double-tap) to factory defaults."""
    for par_name, _label, default in ACTIONS:
        # DEFAULTS are stored platform-neutrally; write the platform form.
        comp.par[par_name] = normalize(default)
    comp.par.Shortcuttagger = TAGGER_DEFAULT
    ui.status = 'Embody: keyboard shortcuts reset to defaults'
    comp.Log('Keyboard shortcuts reset to defaults', 'INFO')
