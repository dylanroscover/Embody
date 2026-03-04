def onOffToOn(panelValue):
	if panelValue.name == 'lselect':
		container = me.par.panels.eval()
		me.parent().ext.ToolbarExt.OnContainerClick(container)

def whileOn(panelValue):
	return

def onOnToOff(panelValue):
	if panelValue.name == 'rollover':
		container = me.par.panels.eval()
		me.parent().ext.ToolbarExt.OnContainerRollover(container, False)

def whileOff(panelValue):
	return

def onValueChange(panelValue, prev):
	if panelValue.name in ('insideu', 'rollover'):
		container = me.par.panels.eval()
		if container.panel.rollover.val:
			me.parent().ext.ToolbarExt.OnContainerRollover(container, True)
