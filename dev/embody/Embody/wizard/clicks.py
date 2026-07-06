def onOffToOn(panelValue):
	op.Embody.op("wizard/logic").module.click(panelValue.owner.name)
	return
def onOnToOff(panelValue):
	n=panelValue.owner.name
	if n.startswith("opt_"):
		op.Embody.op("wizard/logic").module.click(n)
	return
