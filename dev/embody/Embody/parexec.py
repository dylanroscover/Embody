# me - this DAT
# par - the Par object that has changed
# val - the current value
# prev - the previous value
# 
# Make sure the corresponding toggle is enabled in the Parameter Execute DAT.

import webbrowser

def onValueChange(par, prev):
	# use par.eval() to get current value
	if par.name == 'Folder':
		parent.Embody.Disable(prev, removeTags=False)
		run(f"op('{parent.Embody}').UpdateHandler()", delayFrames = 60)

	elif par.name == 'Externalizations':
		if not par:
			parent.Embody.MissingExternalizationsPar()

	elif par.name == 'Envoyenable':
		if par.eval():
			parent.Embody.ext.Envoy.Start()
		else:
			parent.Embody.ext.Envoy.Stop()

	elif par.name == 'Envoyport':
		# Auto-restart server on port change if currently enabled
		if parent.Embody.par.Envoyenable.eval():
			parent.Embody.ext.Envoy.Stop()
			# Delay restart to ensure clean shutdown
			run("parent.Embody.ext.Envoy.Start()", delayFrames=2)

	# UI color pars changed - reload list theme
	elif 'color' in par.name.lower():
		list_comp = op('list/list1')
		if list_comp:
			list_comp.reset()

	elif par.name == 'Embeddatsintdns':
		parent.Embody.ext.TDN.ReexportAllTDNs()

	return

def onPulse(par):
	if par.name == 'Disable':
		parent.Embody.DisableHandler()
		
	elif par.name == 'Update':
		parent.Embody.UpdateHandler()

	elif par.name == 'Refresh':
		parent.Embody.Refresh()

	elif par.name == 'Openmanager':
		parent.Embody.Manager('open')

	elif par.name == 'Closemanager':
		parent.Embody.Manager('close')
				
	elif par.name == 'Github':
		webbrowser.open('https://github.com/dylanroscover/Embody')

	elif par.name == 'Help':
		op('help').openViewer()

	elif par.name == 'Openexternalizationstable':
		parent.Embody.OpenTable()

	elif par.name == 'Externalizeproject':
		parent.Embody.ExternalizeProject()

	elif par.name == 'Exportprojecttdn':
		parent.Embody.ext.TDN.ExportProjectTDNInteractive()

	elif par.name == 'Importtdn':
		file_path = parent.Embody.par.Tdnfile.eval()
		target = parent.Embody.par.Networkpath.eval()
		target_path = str(target) if target else '/'
		clear_first = getattr(parent.Embody.ext.Embody, '_import_clear_first', False)
		parent.Embody.ext.TDN.ImportNetworkFromFile(file_path, target_path, clear_first=clear_first)
		parent.Embody.ext.Embody._import_clear_first = False

	return

def onExpressionChange(par, val, prev):
	return

def onExportChange(par, val, prev):
	return

def onEnableChange(par, val, prev):
	return

def onModeChange(par, val, prev):
	return
	