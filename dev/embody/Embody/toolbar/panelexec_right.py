def onOffToOn(panelValue):
	if panelValue.name == 'lselect':
		container = me.par.panels.eval()
		me.parent().ext.ToolbarExt.OnContainerPress(container)
		me.parent().ext.ToolbarExt.OnContainerClick(container)

def whileOn(panelValue):
	return

def onOnToOff(panelValue):
	if panelValue.name == 'rollover':
		container = me.par.panels.eval()
		me.parent().ext.ToolbarExt.OnContainerRollover(container, False)
	if panelValue.name == 'lselect':
		container = me.par.panels.eval()
		me.parent().ext.ToolbarExt.OnContainerRelease(container)

def whileOff(panelValue):
	return

def onValueChange(panelValue, prev):
	if panelValue.name in ('insideu', 'rollover'):
		container = me.par.panels.eval()
		if container.panel.rollover.val:
			me.parent().ext.ToolbarExt.OnContainerRollover(container, True)
