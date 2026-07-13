# Embody keyboard shortcuts (issue #50): par-driven dispatch + recorder.
#
# Bindings are the Str pars on the Embody COMP's Shortcuts page; the combo
# format, normalization, validation, and recording live in the shortcuts
# module DAT. TD's generated onKey signature (17 args, incl. cmd/lCmd/rCmd
# -- the wiki's 14-arg signature is stale): 'key' is the layout-normalized
# key name and is what shortcuts match on; 'state' is True for key-down.


def _runAction(par_name):
	e = parent.Embody
	if par_name == 'Shortcutmanager':
		e.Manager('open')
	elif par_name == 'Shortcutupdateall':
		e.UpdateHandler()
	elif par_name == 'Shortcutupdatecomp':
		e.SaveCurrentComp()
	elif par_name == 'Shortcutrefresh':
		e.Refresh()
	elif par_name == 'Shortcutexportproject':
		e.ext.TDN.ExportProjectTDNInteractive()
	elif par_name == 'Shortcutexportcomp':
		pane = ui.panes.current
		if pane and pane.owner:
			e.ext.TDN.ExportNetworkAsync(
				root_path=pane.owner.path, output_file='auto')
	elif par_name == 'Shortcutcopytdn':
		e.ext.TDN.CopySelectedToClipboard()


def onKey(dat, key, character, alt, lAlt, rAlt, ctrl, lCtrl, rCtrl, shift, lShift, rShift, state, time, cmd, lCmd, rCmd):

	# Suppress all shortcuts in Perform Mode (belt-and-suspenders -- DAT is also disabled)
	if parent.Embody.par.Performmode.eval():
		return

	sc = mod.shortcuts

	# An armed recorder consumes every key event until commit/cancel/timeout.
	if sc.handleRecordingKey(parent.Embody, key, ctrl, alt, shift, cmd, state):
		return

	if not state:
		return

	# Tagger double-tap (configurable modifier -- not expressible as a combo;
	# Cmd folds onto Ctrl so the stored key works on both platforms)
	tap_key = str(parent.Embody.par.Shortcuttagger.eval())
	if sc.taggerKeyMatches(tap_key, key):
		timer = op('timer1')
		if timer['running']:
			run(f"op('{parent.Embody}').TagGetter()", delayFrames=6)

		timer.par.active = 1
		timer.par.start.pulse()
		return

	if key in sc.MODIFIER_KEYS:
		return

	par_name = sc.actionForEvent(parent.Embody, key, ctrl, alt, shift, cmd)
	if par_name:
		_runAction(par_name)

	return

# shortcutName is the name of the shortcut

def onShortcut(dat, shortcutName, time):
	return;
