# me - this DAT
# 
# frame - the current frame
# state - True if the timeline is paused
# 
# Make sure the corresponding toggle is enabled in the Execute DAT.

def init():
	# Log version info for debugging user issues
	parent.Embody.Log(
		f"Embody v{parent.Embody.par.Version.eval()} | "
		f"TouchDesigner {app.version}.{app.build} | "
		f"{app.osName} {app.osVersion}"
	)
	# Prevent Envoy from auto-starting before init completes.
	# The release .tox may bake in Envoyenable=True and Envoystatus=Running;
	# reset both so the git dialog doesn't fire before the externalizations
	# table is ready and Start() isn't blocked by a stale status.
	# Suppress parexec side effects (settings save, Start trigger) during reset.
	parent.Embody.par.Envoyenable = False
	parent.Embody.par.Envoystatus = 'Disabled'
	# Signal parexec that init is done -- safe to process param changes.
	# Before this flag, parexec suppresses all side effects because .tox
	# param loading fires onValueChange BEFORE init() runs.
	parent.Embody.ext.Embody._init_complete = True


def onStart():
	init()
	# Restore settings from .embody.json — recovers user config after
	# crash, force-quit, or any unsaved session. On normal open where
	# .toe was saved, values match and this is a no-op.
	run(f"op('{parent.Embody}').ext.Embody._restoreSettings(kick_envoy=True)", delayFrames=5)
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
	# Clear runtime-only storage that must not bake into the .tox.
	# _git_root is computed fresh at Start() time — baking it in would cause
	# every user's project to inherit the dev repo path from the release .tox.
	parent.Embody.unstore('_git_root')
	# Clear session-only stores before the save so they never bake into the
	# .tox. _tdn_stripped_paths and _tdn_pane_restore are written and consumed
	# within a single Ctrl+S cycle — they have no meaning across sessions.
	parent.Embody.unstore('_tdn_stripped_paths')
	parent.Embody.unstore('_tdn_pane_restore')

	# Suppress the delayed Refresh pulse - the continuity check must NOT
	# fire during the strip/restore window or it will delete files for
	# temporarily-missing operators inside TDN COMPs.
	parent.Embody.ext.Embody.Update(suppress_refresh=True)

	# DAT content safety — detect unprotected DATs before strip/restore
	parent.Embody.ext.Embody._checkDATContentSafety()

	tdn_comps = parent.Embody.ext.Embody._getTDNStrategyComps()
	if not tdn_comps:
		return

	# Phase 1: Export current in-memory state to .tdn files, but only
	# if the content actually changed. Skipping unchanged COMPs avoids
	# noisy git diffs from volatile header fields (build, generator,
	# exported_at, td_build).
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

			# Export to dict only (no file write yet)
			result = parent.Embody.ext.TDN.ExportNetwork(
				root_path=comp_path, output_file=None)
			if not result.get('success'):
				parent.Embody.ext.Embody.Log(
					f'Pre-save export failed for {comp_path}: '
					f'{result.get("error")}', 'ERROR')
				continue

			new_tdn = result['tdn']

			# Compare against existing file â€” skip write if content unchanged
			existing_tdn = parent.Embody.ext.TDN._read_existing_tdn(abs_path)
			if existing_tdn and parent.Embody.ext.TDN._tdn_content_equal(
					new_tdn, existing_tdn):
				exported.append((comp_path, rel_tdn_path))
				continue

			# Content changed (or first export) â€” write to disk
			scan_folder = str(project.folder)
			before_tdn = parent.Embody.ext.TDN._collectExistingTDNFiles(
				scan_folder, comp_path)
			content = parent.Embody.ext.TDN._compact_json_dumps(new_tdn)
			write_result = parent.Embody.ext.TDN._safe_write_tdn(
				abs_path, content, scan_folder)
			if not write_result.get('success'):
				parent.Embody.ext.Embody.Log(
					f'Pre-save write failed for {comp_path}: '
					f'{write_result.get("error")}', 'ERROR')
				continue

			# Stale file cleanup
			protected = [abs_path]
			other_protected = parent.Embody.ext.Embody._getAllTrackedTDNFiles(
				exclude_path=comp_path)
			if other_protected:
				protected.extend(other_protected)
			parent.Embody.ext.TDN._cleanupStaleTDNFiles(
				before_tdn, protected, scan_folder)

			# Track export and update fingerprint
			parent.Embody.ext.TDN._trackTDNExport(
				comp_path, abs_path,
				build_num=new_tdn.get('build'),
				touch_build=f'{app.version}.{app.build}')
			parent.Embody.ext.Embody._storeTDNFingerprint(comp)
			exported.append((comp_path, rel_tdn_path))
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

	# Save pane owner paths that fall inside TDN COMPs before stripping.
	# After restore, we re-navigate orphaned panes back to the rebuilt COMP.
	tdn_paths = [cp for cp, _ in exported]
	pane_restore = {}
	try:
		for pane in ui.panes:
			if hasattr(pane, 'owner') and pane.owner:
				owner_path = pane.owner.path
				for tp in tdn_paths:
					if owner_path == tp or owner_path.startswith(tp + '/'):
						pane_restore[pane.id] = owner_path
						break
	except Exception:
		pass  # Non-critical — pane restoration is best-effort
	if pane_restore:
		parent.Embody.store('_tdn_pane_restore', pane_restore)

	# Sort deepest-first so nested TDN COMPs (e.g. /META/geo1) are
	# stripped before their parent (/META) destroys them. Without this,
	# the parent's StripCompChildren destroys the nested COMP before it
	# can be tracked in stripped_info, so post-save never restores it.
	exported_by_depth = sorted(
		exported, key=lambda x: x[0].count('/'), reverse=True)
	stripped_info = []
	for comp_path, rel_tdn_path in exported_by_depth:
		comp = op(comp_path)
		if comp:
			parent.Embody.ext.Embody.StripCompChildren(comp)
		# Always track — nested COMPs may already be destroyed by a
		# parent strip earlier in this loop. They still need restoring.
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
	# Sort shallowest-first so parent COMPs (e.g. /META) are restored
	# before their nested children (/META/geo1). The parent import
	# recreates the child COMP shell; the child import then replaces
	# its default contents (e.g. Torus) with the correct .tdn state.
	def _depth_key(entry):
		p = entry[0] if isinstance(entry, (list, tuple)) else entry
		return p.count('/')
	stripped = sorted(stripped, key=_depth_key)
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
			# Attempt rollback from backup .tdn
			try:
				backup_path = parent.Embody.ext.TDN._get_backup_path_instance(
					str(abs_path))
				if backup_path.is_file():
					import json as _json
					backup_tdn = _json.loads(
						backup_path.read_text(encoding='utf-8'))
					parent.Embody.ext.TDN.ImportNetwork(
						target_path=comp_path, tdn=backup_tdn,
						clear_first=True, restore_file_links=True)
					print(f'Embody > Rolled back {comp_path} from backup')
			except Exception as rb_e:
				print(f'Embody > Rollback also failed for {comp_path}: {rb_e}')
	# Restore pane owners that were orphaned during strip
	pane_restore = parent.Embody.fetch('_tdn_pane_restore', {}, search=False)
	if pane_restore:
		parent.Embody.unstore('_tdn_pane_restore')
		try:
			for pane in ui.panes:
				saved_path = pane_restore.get(pane.id)
				if saved_path:
					target = op(saved_path)
					if target:
						pane.owner = target
		except Exception:
			pass  # Non-critical — pane restoration is best-effort

	# Safe to refresh now - all stripped COMPs have been restored
	run(f"op('{parent.Embody}').par.Refresh.pulse()", delayFrames=1)
	return
