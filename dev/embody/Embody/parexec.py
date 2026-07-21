# me - this DAT
# par - the Par object that has changed
# val - the current value
# prev - the previous value
# 
# Make sure the corresponding toggle is enabled in the Parameter Execute DAT.

import webbrowser

def onValueChange(par, prev):
	# Suppress all side effects during init and settings restore.
	# On .tox load, parameters are set from baked values BEFORE
	# init()/onCreate() runs -- without this guard, parexec writes
	# .embody/config.json with baked values before init() can intervene.
	ext = parent.Embody.ext.Embody
	if getattr(ext, '_restoring_settings', False):
		return
	if not parent.Embody.fetch('_init_complete', False, search=False):
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

	elif par.name == 'Aiprojectroot':
		# Move Embody's own state (.embody/config.json, project.json) to the
		# new root first so _saveSettings (triggered by _deferSaveSettings
		# below) writes alongside the migrated file rather than next to a
		# stale copy. Then regenerate AI/MCP config at the new root.
		new_mode = par.eval()
		parent.Embody.ext.Embody._migrateRootFiles(prev, new_mode)
		# Toggle enable on the custom-path sibling param so it greys out
		# when the menu isn't on 'custom'.
		custom_par = getattr(parent.Embody.par, 'Aiprojectrootcustom', None)
		if custom_par is not None:
			custom_par.enable = (new_mode == 'custom')
		if parent.Embody.par.Envoyenable.eval():
			parent.Embody.InitEnvoy()

	elif par.name == 'Aiprojectrootcustom':
		# Custom path changed within 'custom' mode -- migrate from old path
		# to new path. No-op if Aiprojectroot isn't currently 'custom'
		# (the value is preserved but inactive until the menu flips back).
		if parent.Embody.par.Aiprojectroot.eval() == 'custom':
			parent.Embody.ext.Embody._migrateRootFiles(
				'custom', 'custom', old_custom=prev, new_custom=par.eval())
			if parent.Embody.par.Envoyenable.eval():
				parent.Embody.InitEnvoy()

	elif par.name == 'Envoyenable':
		if par.eval():
			# Defer Start and re-check -- gives init() time to suppress
			# the baked-in Envoyenable=True before the server launches.
			# The 30-frame delay matches Verify() timing in onCreate().
			run("parent.Embody.ext.Envoy.Start() if parent.Embody.par.Envoyenable.eval() else None",
				delayFrames=30)
		else:
			parent.Embody.ext.Envoy.Stop()

	elif par.name == 'Performmode':
		if par.eval():
			parent.Embody.ext.Embody._enterPerformMode()
		else:
			parent.Embody.ext.Embody._exitPerformMode()

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

	elif par.name == 'Tdnmode':
		parent.Embody.ext.Embody._onTdnModeChanged(str(par.eval()))

	elif par.name == 'Embeddatsintdns':
		# No-op when the TDN subsystem is disabled -- nothing to re-export.
		if parent.Embody.ext.Embody._tdnEnabled():
			parent.Embody.ext.TDN.ReexportAllTDNs()

	elif par.name == 'Embedstorageintdns':
		# No-op when the TDN subsystem is disabled -- nothing to re-export.
		if parent.Embody.ext.Embody._tdnEnabled():
			parent.Embody.ext.TDN.ReexportAllTDNs()

	elif par.name == 'Tdncascade':
		state = 'enabled' if par.eval() else 'disabled'
		parent.Embody.ext.Embody.Log(f'TDN cascade {state}', 'INFO')

	elif par.name in mod.shortcuts.SHORTCUT_PARS:
		# Normalize hand-typed combos to the canonical form; revert invalid
		# input. Rewriting par.val re-fires this handler once -- the second
		# pass is a no-op because normalize() is idempotent.
		sc = mod.shortcuts
		raw = str(par.eval())
		norm = sc.normalize(raw)
		if norm is None:
			ui.status = (f"Embody: invalid shortcut '{raw}' for {par.label} "
				"(use e.g. ctrl+shift+o) -- reverted")
			parent.Embody.Log(
				f"Invalid shortcut '{raw}' for {par.label} -- reverted", 'WARNING')
			# The revert target must itself be normalize-stable, or two
			# invalid values would ping-pong through this handler forever
			# (prev can be garbage if a corrupted config was restored while
			# this handler was suppressed). Fall back to the factory default.
			fallback = sc.normalize(prev) if prev is not None else None
			if fallback is None:
				fallback = sc.normalize(sc.DEFAULTS.get(par.name, '')) or ''
			par.val = fallback
		elif norm != raw:
			par.val = norm
		else:
			# A combo may drive only ONE action: reject duplicates outright.
			dup = sc.duplicateOf(parent.Embody, par.name, norm)
			if dup is not None:
				ui.status = (f'Embody: {sc.display(norm)} is already assigned '
					f"to '{sc.actionLabel(dup)}' -- reverted")
				parent.Embody.Log(
					f'Duplicate shortcut {norm} for {par.label} '
					f'(held by {sc.actionLabel(dup)}) -- reverted', 'WARNING')
				fallback = sc.normalize(prev) if prev is not None else ''
				if fallback is None or (fallback and
						sc.duplicateOf(parent.Embody, par.name, fallback)):
					fallback = ''
				par.val = fallback
			else:
				for w in sc.validate(parent.Embody, par.name, norm):
					ui.status = f'Embody: {w}'
					parent.Embody.Log(f'Shortcut warning ({par.label}): {w}', 'WARNING')

	if par.name in parent.Embody.ext.Embody._PERSISTED_PARAMS:
		# Skip persistence while the test runner is flipping params for a
		# run (it suppresses Toxdropexpr/Filecleanup and restores them
		# after) -- a TD kill mid-run must not bake a suppressed value
		# into config.json and silently convert the user's preference.
		if not parent.Embody.ext.Embody._testRunnerActive():
			parent.Embody.ext.Embody._deferSaveSettings()

	return

def onPulse(par):
	if par.name == 'Disable':
		parent.Embody.DisableHandler()

	elif par.name == 'Uninstall':
		parent.Embody.UninstallHandler()

	elif par.name == 'Update':
		parent.Embody.UpdateHandler()

	elif par.name == 'Checkforupdate':
		# Self-update check (UpdaterExt on the updater child COMP) --
		# distinct from 'Update', which re-exports externalizations.
		# Guarded like execute.py's startup hook: tolerate a partial /
		# pre-updater install where the par exists but the child does not.
		updater = parent.Embody.op('updater')
		if updater:
			updater.ext.UpdaterExt.CheckForUpdate(interactive=True)
		else:
			parent.Embody.Log(
				'Check for Update: updater component missing '
				'(reinstall Embody to enable self-update)', 'WARNING')

	elif par.name == 'Refresh':
		parent.Embody.Refresh()

	elif par.name == 'Setupwizard':
		parent.Embody.ext.Embody._openSetupWizard()

	elif par.name == 'Openmanager':
		parent.Embody.Manager('open')

	elif par.name == 'Closemanager':
		parent.Embody.Manager('close')
				
	elif par.name == 'Launchaiclient':
		parent.Embody.LaunchAIClient()

	elif par.name == 'Github':
		webbrowser.open('https://github.com/dylanroscover/Embody')

	elif par.name == 'Help':
		# text_help is the synced template (carries a {{VERSION}} token);
		# render it into the non-synced display DAT with the live version so
		# the header can never drift, then open the opviewer panel.
		tpl = op('help/text_help')
		view = op('help/text_help_display')
		if tpl is not None and view is not None:
			import re
			sc = mod.shortcuts
			ver = str(parent.Embody.par.Version.eval())
			text = tpl.text.replace('{{VERSION}}', ver)
			text = text.replace('{{SHORTCUTS}}', sc.helpBlock(parent.Embody))
			text = text.replace('{{TAGGERTAP}}',
				sc.taggerTapDisplay(parent.Embody))
			text = re.sub(r'\{\{SC:(\w+)\}\}',
				lambda mt: sc.display(parent.Embody.par[mt.group(1)].eval()),
				text)
			view.text = text
		op('help').openViewer()

	elif par.name == 'Openexternalizationstable':
		parent.Embody.OpenTable()

	elif par.name == 'Createexternalizationstable':
		parent.Embody.ext.Embody.CreateExternalizationsTable()

	elif par.name == 'Externalizeproject':
		parent.Embody.ExternalizeProject()

	elif par.name in mod.shortcuts.RECORD_PARS:
		mod.shortcuts.arm(parent.Embody, mod.shortcuts.RECORD_PARS[par.name])

	elif par.name == 'Resetshortcuts':
		mod.shortcuts.resetDefaults(parent.Embody)

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
	