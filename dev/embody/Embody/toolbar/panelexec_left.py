def onOffToOn(panelValue):
	if panelValue.name == 'lselect':
		container = me.par.panels.eval()
		me.parent().ext.ToolbarExt.onContainerPress(container)
		me.parent().ext.ToolbarExt.onContainerClick(container)

def whileOn(panelValue):
	return

def onOnToOff(panelValue):
	if panelValue.name == 'rollover':
		container = me.par.panels.eval()
		me.parent().ext.ToolbarExt.onContainerRollover(container, False)
	if panelValue.name == 'lselect':
		container = me.par.panels.eval()
		me.parent().ext.ToolbarExt.onContainerRelease(container)

def whileOff(panelValue):
	return

def onValueChange(panelValue, prev):
	if panelValue.name in ('insideu', 'rollover'):
		container = me.par.panels.eval()
		if container.panel.rollover.val:
			me.parent().ext.ToolbarExt.onContainerRollover(container, True)
