def onOffToOn(panelValue):
	if panelValue.name == 'lselect':
		me.parent().ext.WindowHeaderExt.OnClick()

def whileOn(panelValue):
	return

def onOnToOff(panelValue):
	if panelValue.name == 'rollover':
		me.parent().ext.WindowHeaderExt.OnRollover(False)

def whileOff(panelValue):
	return

def onValueChange(panelValue, prev):
	if panelValue.name in ('insideu', 'rollover'):
		if me.parent().panel.rollover.val:
			me.parent().ext.WindowHeaderExt.OnRollover(True)
