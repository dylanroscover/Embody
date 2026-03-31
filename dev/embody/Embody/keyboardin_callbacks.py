# me - This DAT
# 
# dat - The DAT that received the key event
# key - The name of the key attached to the event.
#		This tries to be consistent regardless of which language
#		the keyboard is set to. The values will be the english/ASCII
#		values that most closely match the key pressed.
#		This is what should be used for shortcuts instead of 'character'.
# character - The unicode character generated.
# alt - True if the alt modifier is pressed
# ctrl - True if the ctrl modifier is pressed
# shift - True if the shift modifier is pressed
# state - True if the event is a key press event
# time - The time when the event came in milliseconds
# cmd - True if the cmd modifier is pressed




def onKey(dat, key, character, alt, lAlt, rAlt, ctrl, lCtrl, rCtrl, shift, lShift, rShift, state, time, cmd, lCmd, rCmd):
	
	# Combine ctrl and cmd for cross-platform compatibility
	ctrl_or_cmd = ctrl or cmd

	# view externalizations
	if state and key == 'o' and ctrl_or_cmd and lShift:
		parent.Embody.Manager('open')

	# add tox/dat tag
	elif state and key == 'lctrl':
		timer = op('timer1')
		if timer['running']:
			run(f"op('{parent.Embody}').TagGetter()", delayFrames=6)

		timer.par.active = 1
		timer.par.start.pulse()

	# initialize/update externalizations
	elif state and key == 'u' and ctrl_or_cmd and lShift:
		parent.Embody.UpdateHandler()

	# Refresh tracking state
	elif state and key == 'r' and ctrl_or_cmd and lShift:
		parent.Embody.Refresh()

	# Update current COMP only
	elif state and key == 'u' and ctrl_or_cmd and alt:
	    parent.Embody.SaveCurrentComp()

	# Export project network to .tdn
	elif state and key == 'e' and ctrl_or_cmd and lShift:
		parent.Embody.ext.TDN.ExportProjectTDNInteractive()

	# Export current COMP to .tdn
	elif state and key == 'e' and ctrl_or_cmd and alt:
		pane = ui.panes.current
		if pane and pane.owner:
			parent.Embody.ext.TDN.ExportNetworkAsync(
				root_path=pane.owner.path, output_file='auto')

	return

# shortcutName is the name of the shortcut

def onShortcut(dat, shortcutName, time):
	return;
	