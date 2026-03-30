# me - this DAT
# par - the Par object that has changed
# val - the current value
# prev - the previous value
# 
# Make sure the corresponding toggle is enabled in the Parameter Execute DAT.

import webbrowser

def onValueChange(par, prev):
	# Suppress all side effects while restoring settings from disk
	if getattr(parent.Embody.ext.Embody, '_restoring_settings', False):
		return

	# use par.eval() to get current value
	if par.name == 'Folder':
		parent.Embody.Disable(prev, removeTags=False)
		run(f"op('{parent.Embody}').UpdateHandler()", delayFrames = 60)

	elif par.name == 'Externalizations':
		if not par:
			parent.Embody.MissingExternalizationsPar()

	elif par.name == 'Aiclient':
		if parent.Embody.par.Envoyenable.eval():
			op.Embody.ext.Embody._extractAIConfig()

	elif par.name == 'Envoyenable':
		if par.eval():
			# Defer Start and re-check â€” gives onCreate time to suppress
			# the baked-in Envoyenable=True before the server launches.
			run("parent.Embody.ext.Envoy.Start() if parent.Embody.par.Envoyenable.eval() else None",
				delayFrames=5)
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

	elif par.name == 'Localtimestamps':
		list_comp = op('list/list1')
		if list_comp:
			list_comp.reset()

	elif par.name == 'Embeddatsintdns':
		parent.Embody.ext.TDN.ReexportAllTDNs()

	elif par.name == 'Embedstorageintdns':
		parent.Embody.ext.TDN.ReexportAllTDNs()

	elif par.name == 'Tdncascade':
		state = 'enabled' if par.eval() else 'disabled'
		parent.Embody.ext.Embody.Log(f'TDN cascade {state}', 'INFO')

	if par.name in parent.Embody.ext.Embody._PERSISTED_PARAMS:
		parent.Embody.ext.Embody._deferSaveSettings()

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

	elif par.name == 'Createexternalizationstable':
		parent.Embody.ext.Embody.CreateExternalizationsTable()

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
	