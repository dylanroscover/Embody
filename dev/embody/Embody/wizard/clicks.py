def onOffToOn(panelValue):
	op.Embody.op("wizard/logic").module.click(panelValue.owner.name)
	return
def onOnToOff(panelValue):
	n=panelValue.owner.name
	# opt_savenow is an ACTION card (opens the OS save dialog), not a
	# selection: it must fire exactly once per click, on the press edge
	# only -- the release edge would reopen a canceled dialog.
	if n.startswith("opt_") and n!="opt_savenow":
		op.Embody.op("wizard/logic").module.click(n)
	return
