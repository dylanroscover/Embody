def onOffToOn(panelValue):
	if panelValue.name == 'lselect':
		me.parent().ext.WindowHeaderExt.onPress()
		me.parent().ext.WindowHeaderExt.onClick()

def whileOn(panelValue):
	return

def onOnToOff(panelValue):
	if panelValue.name == 'rollover':
		me.parent().ext.WindowHeaderExt.onRollover(False)
	if panelValue.name == 'lselect':
		me.parent().ext.WindowHeaderExt.onRelease()

def whileOff(panelValue):
	return

def onValueChange(panelValue, prev):
	if panelValue.name in ('insideu', 'rollover'):
		if me.parent().panel.rollover.val:
			me.parent().ext.WindowHeaderExt.onRollover(True)
