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
	# Reconstruct TDN-strategy COMPs from .tdn files
	run(f"op('{parent.Embody}').ReconstructTDNComps()", delayFrames=60)
	return

def onCreate():
	init()
	run(f"op('{parent.Embody}').Verify()", delayFrames = 30)

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
	# Suppress the delayed Refresh pulse — the continuity check must NOT
	# fire during the strip/restore window or it will delete files for
	# temporarily-missing operators inside TDN COMPs.
	parent.Embody.ext.Embody.Update(suppress_refresh=True)
	# Strip children from TDN-strategy COMPs so the .toe stays small.
	# The children are reconstructed from .tdn on next project open.
	tdn_comps = parent.Embody.ext.Embody._getTDNStrategyComps()
	stripped_info = []
	for comp_path, rel_tdn_path in tdn_comps:
		comp = op(comp_path)
		if comp and comp.children:
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
			# print() as backup — Log may fail if extensions are reinitializing
			print(f'Embody > Post-save restore failed for {comp_path}: {e}')
			try:
				parent.Embody.ext.Embody.Log(
					f'Post-save restore failed for {comp_path}: {e}', 'ERROR')
			except Exception:
				pass
	# Safe to refresh now — all stripped COMPs have been restored
	run(f"op('{parent.Embody}').par.Refresh.pulse()", delayFrames=1)
	return
