# me - this DAT
# par - the Par object that has changed

def onValueChange(par, prev):
	if par:
		comp = None
		for pane in ui.panes:
			if pane.type == PaneType.NETWORKEDITOR:
				comp = pane.owner
				break
		if comp:
			parent.Embody.ext.TDN.ExportNetworkAsync(
				root_path=comp.path, output_file="auto")
	return
