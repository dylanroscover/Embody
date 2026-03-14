# me - this DAT
# 
# frame - the current frame
# state - True if the timeline is paused
# 
# Make sure the corresponding toggle is enabled in the Execute DAT.

def init():
	
	pass
	

def onStart():
	init()
	# On project open, silently extract CLAUDE.md if Envoy is
	# enabled but the file is missing (handles upgrades from older versions)
	run(f"op('{parent.Embody}').ext.Embody._upgradeEnvoy()", delayFrames=30)
	# Restore missing TOX-strategy COMPs from .tox files on disk
	run(f"op('{parent.Embody}').RestoreTOXComps()", delayFrames=45)
	# Restore missing standalone DATs from externalized files on disk
	run(f"op('{parent.Embody}').RestoreDATs()", delayFrames=50)
	# Reconstruct TDN-strategy COMPs from .tdn files
	run(f"op('{parent.Embody}').ReconstructTDNComps()", delayFrames=60)
	# Reconcile metadata for operators that exist but lost tags/colors/file params
	run(f"op('{parent.Embody}').ext.Embody.ReconcileMetadata()", delayFrames=75)
	return

def onCreate():
	init()
	# Prevent Envoy from auto-starting before init completes.
	# The release .tox may bake in Envoyenable=True; reset it so the git
	# dialog doesn't fire before the externalizations table is ready.
	parent.Embody.par.Envoyenable = False
	# Auto-create (or reconnect) the externalizations table before Verify()
	run(f"op('{parent.Embody}').ext.Embody.CreateExternalizationsTable()", delayFrames=15)
	# Verify handles update-scenario detection and Envoy opt-in
	run(f"op('{parent.Embody}').Verify()", delayFrames=30)
	return

def onExit():
	return

def onFrameStart(frame):
	return

def onFrameEnd(frame):
	return

def onPlayStateChange(state):
	return

def onDeviceChange():
	return

def onProjectPreSave():
	# Suppress the delayed Refresh pulse - the continuity check must NOT
	# fire during the strip/restore window or it will delete files for
	# temporarily-missing operators inside TDN COMPs.
	parent.Embody.ext.Embody.Update(suppress_refresh=True)

	tdn_comps = parent.Embody.ext.Embody._getTDNStrategyComps()
	if not tdn_comps:
		return

	# Phase 1: Export current in-memory state to .tdn files.
	# This ensures Ctrl+S actually saves any changes the user made
	# (positions, parameters, annotations, new operators, etc.).
	exported = []
	for comp_path, rel_tdn_path in tdn_comps:
		comp = op(comp_path)
		if not comp:
			continue
		# Skip empty COMPs - nothing to export or strip
		has_children = bool(comp.findChildren(depth=1, includeUtility=True))
		if not has_children:
			continue
		try:
			abs_path = str(parent.Embody.ext.Embody.buildAbsolutePath(rel_tdn_path))
			protected = parent.Embody.ext.Embody._getAllTrackedTDNFiles(
				exclude_path=comp_path)
			result = parent.Embody.ext.TDN.ExportNetwork(
				root_path=comp_path, output_file=abs_path,
				cleanup_protected=protected)
			if result.get('success'):
				exported.append((comp_path, rel_tdn_path))
				parent.Embody.ext.Embody._storeTDNFingerprint(comp)
			else:
				parent.Embody.ext.Embody.Log(
					f'Pre-save export failed for {comp_path}: '
					f'{result.get("error")}', 'ERROR')
		except Exception as e:
			parent.Embody.ext.Embody.Log(
				f'Pre-save export error for {comp_path}: {e}', 'ERROR')

	# Phase 2: Strip children from exported COMPs so the .toe stays small.
	# Only strip COMPs whose export succeeded - stripping without a valid
	# .tdn on disk would permanently destroy the children.
	# Gated on Strip on Save toggle - when Off, .tdn files are still exported
	# (Phase 1) but children stay in the .toe.
	if not parent.Embody.par.Tdnstriponsave.eval():
		return

	stripped_info = []
	for comp_path, rel_tdn_path in exported:
		comp = op(comp_path)
		if comp:
			parent.Embody.ext.Embody.StripCompChildren(comp)
			stripped_info.append((comp_path, rel_tdn_path))
	if stripped_info:
		parent.Embody.store('_tdn_stripped_paths', stripped_info)
	return

def onProjectPostSave():
	# Restore children that were stripped during pre-save.
	# Re-import from the just-exported .tdn files to keep the session intact.
	stripped = parent.Embody.fetch('_tdn_stripped_paths', [], search=False)
	if not stripped:
		return
	parent.Embody.unstore('_tdn_stripped_paths')
	for entry in stripped:
		# Unpack stored (comp_path, rel_tdn_path) tuples.
		# Fall back to legacy format (plain string) for safety.
		if isinstance(entry, (list, tuple)) and len(entry) == 2:
			comp_path, rel_path = entry
		else:
			comp_path = entry
			try:
				rel_path = parent.Embody.ext.Embody._getStrategyFilePath(comp_path, 'tdn')
			except Exception:
				rel_path = None
		try:
			if not rel_path:
				parent.Embody.ext.Embody.Log(
					f'Post-save restore: no TDN file path for {comp_path}', 'WARNING')
				continue
			abs_path = parent.Embody.ext.Embody.buildAbsolutePath(rel_path)
			if not abs_path.is_file():
				parent.Embody.ext.Embody.Log(
					f'Post-save restore: .tdn file missing: {rel_path}', 'WARNING')
				continue
			import json
			tdn_doc = json.loads(abs_path.read_text(encoding='utf-8'))
			parent.Embody.ext.TDN.ImportNetwork(
				target_path=comp_path, tdn=tdn_doc, clear_first=True,
				restore_file_links=True)
		except Exception as e:
			# print() as backup - Log may fail if extensions are reinitializing
			print(f'Embody > Post-save restore failed for {comp_path}: {e}')
			try:
				parent.Embody.ext.Embody.Log(
					f'Post-save restore failed for {comp_path}: {e}', 'ERROR')
			except Exception:
				pass
	# Safe to refresh now - all stripped COMPs have been restored
	run(f"op('{parent.Embody}').par.Refresh.pulse()", delayFrames=1)
	return
