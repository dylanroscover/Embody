SURF=(0.16,0.17,0.165)
BACK_ON=(0.19,0.20,0.195); NEXT_ON=(0.24,0.52,0.35); NEXT_OFF=(0.135,0.15,0.14)
TXT=(0.92,0.92,0.92); TXT_DIM=(0.34,0.35,0.34); NEXT_TXT=(0.97,0.99,0.97)
GROUPS=['grp_save','grp_mode','grp_assistant','grp_client','grp_permissions','grp_convoy','grp_git','grp_footprint','grp_externalize']
DEFS={
 'save':{'g':'grp_save','sel':None,'title':'Save your project first','hint':"Embody creates its Python env (.venv), AI config, and .embody state in your project's folder -- and an unsaved project has no folder yet. Save first so everything lands beside your .toe.",'hintfn':'_saveHint'},
 'mode':{'g':'grp_mode','sel':'sel_mode','title':'How should Embody manage your project?','hint':'Choose one, then Next.'},
 'convoy':{'g':'grp_convoy','sel':'sel_convoy','title':'Enable Convoy?','hint':'Connects this Embody with other Convoy-enabled instances on the same trusted LAN, so they can discover and control each other. Enable only on a network you trust. Convoy installs a small background app so nodes stay reachable while TouchDesigner is closed.','hintfn':'_convoyHint'},
 'externalize':{'g':'grp_externalize','sel':'sel_externalize','title':'Make your project AI-readable?','hint':'Write your network to diffable files git and AI tools can read.'},
 'assistant':{'g':'grp_assistant','sel':'sel_assistant','title':'Turn on the AI assistant (Envoy)?','hint':'It lets AI tools work in your network. Easy to remove later.'},
 'client':{'g':'grp_client','sel':'sel_client','title':'Pick your AI coding tool','hint':'Embody will generate its config.'},
 'git':{'g':'grp_git','sel':'sel_git','title':'Make this project a Git repository?','hint':'Externalized files diff and restore best under version control.'},
 'footprint':{'g':'grp_footprint','sel':'sel_root','title':'Review what Embody will add','hint':'Embody will add a Python env (.venv) + MCP server, config files (.mcp.json, .embody, TDPyEnvManagerContext.yaml, AI rules), .gitignore/.gitattributes entries + a .tdn diff driver, and the Embot assistant in your network. Everything is recorded and reversible via Uninstall.'},
 'permissions':{'g':'grp_permissions','sel':'sel_permissions','title':'How should the AI ask permission?','hint':'Claude Code asks before each AI tool by default. Pick how much to auto-approve (changeable later on the Envoy parameters).'},
 'summary':{'g':None,'sel':None,'title':'Ready to set up Embody','hint':''},
}
def _w(): return parent()
def _bg(o,c):
	if o:
		for i,ch in enumerate('rgb'): setattr(o.par,'bgcolor'+ch,c[i])
def _tc(o,c):
	if not o: return
	for pref in ('fontcolorr','textcolorr'):
		if hasattr(o.par,pref):
			b=pref[:-1]
			for i,ch in enumerate('rgb'): setattr(o.par,b+ch,c[i])
			return
def spine():
	w=_w(); m=w.fetch('sel_mode',None); a=w.fetch('sel_assistant',None)
	# The save gate comes BEFORE everything: .venv, AI config, .embody
	# state, and the optional git repo all land relative to the project
	# folder, so an unsaved project would scatter them into TD's default
	# location. Probed once at start(), so the step (and numbering) is
	# stable for the whole session even after the user saves mid-wizard.
	s=(['save'] if w.fetch('needs_save',False) else [])+['mode']
	# Externalization offer, right after the posture step. Two gates, both
	# cheap: the option group must EXIST (older wizard panels have no
	# grp_externalize -- including it then would strand Next, which stays
	# disabled until a group option is picked), and the project must still
	# have something to externalize (probed once at start()).
	if w.op('grp_externalize') and w.fetch('ext_needed',True): s.append('externalize')
	s.append('assistant')
	if a=='other': s.append('client')
	if a=='claudecode': s.append('permissions')
	# Convoy is also a TouchDesigner-originated sibling API, so it is independent
	# of the AI-client choice. Default is DISABLED -- turning on remote control
	# is an explicit, trusted-LAN choice; enabling installs and starts the
	# host app automatically, consented by this step's answer.
	s.append('convoy')
	# Git decision only when no repo was found at wizard start -- and for
	# EVERY assistant choice incl. 'none' (externalization is git's whole
	# point; the decision is about the project, not the AI).
	if w.fetch('git_missing',False): s.append('git')
	if m=='advanced' and (a not in (None,'none') or w.fetch('sel_convoy','')=='enable'): s.append('footprint')
	s.append('summary'); return s
def _grp(step): return _w().op(DEFS.get(step,{}).get('g') or '')
def _chosen(step):
	g=_grp(step)
	if not g: return None
	for c in g.children:
		if c.family=='COMP' and hasattr(c.par,'value0') and c.par.value0.eval(): return c.name.replace('opt_','')
	return None
def _nav(k):
	for c in _w().op('footer').children:
		if c.family=='COMP' and k in c.name: return c
def _recap():
	w=_w(); m=w.fetch('sel_mode','auto'); a=w.fetch('sel_assistant','claudecode')
	g=''
	if w.fetch('git_missing',False):
		g={'gitinit':' Git: initialize here.','gitskip':' Git: skipped for now.'}.get(w.fetch('sel_git',''),'')
	# The externalize choice is the only one that can rewrite the WHOLE
	# project, so the summary always states it back before Set up Embody.
	if 'externalize' in spine():
		g+={'full':' Externalize: whole project now, plus new work.','auto':' Externalize: new work from here on.','skip':' Externalize: skipped for now.'}.get(w.fetch('sel_externalize',''),'')
	if 'convoy' in spine():
		g+={'enable':' Convoy: enabled.','disable':' Convoy: off.'}.get(w.fetch('sel_convoy',''),'')
	if a=='none': return 'Mode: %s. AI assistant: off.%s\nNothing has changed yet - click Set up Embody to apply.'%(m,g)
	c=w.fetch('sel_client','') if a=='other' else ('Claude Code' if a=='claudecode' else a)
	return 'Mode: %s. AI assistant: on (%s).%s\nNothing has changed yet - click Set up Embody to apply.'%(m, c or 'your tool', g)
def _permHint():
	base=DEFS['permissions']['hint']
	try:
		from pathlib import Path
		root=op.Embody.ext.Embody._findProjectRoot()
		if root and (Path(str(root))/'.claude'/'settings.local.json').exists():
			base+='\nA settings.local.json already exists -- Embody edits only its Envoy entries, keeping the rest.'
	except Exception: pass
	return base
def _projectSaved():
	"""A Convoy node is identified by its project folder, so an unsaved
	project cannot become one -- enabling would flip itself back off.

	One authority for saved-ness: EmbodyExt._projectSavedOnDisk (TD's
	'NewProject[.N].toe' placeholder name = never saved). Testing for a file
	at project.folder / project.name is WRONG -- TD reports the next
	incremental name after a save, so that path is missing on projects that
	have been saved for months."""
	try:
		return bool(op.Embody.ext.Embody._projectSavedOnDisk())
	except Exception:
		pass
	try:
		import os
		return bool(project.name) and os.path.isfile(
			os.path.join(str(project.folder), str(project.name)))
	except Exception:
		return False
def _saveHint():
	if _projectSaved():
		try: return 'Saved. Embody will set up inside %s -- click Next.'%(str(project.folder))
		except Exception: return 'Saved -- click Next.'
	return DEFS['save']['hint']
def _saveProjectNow():
	"""The wizard's step-1 save: the OS save dialog, then project.save().

	Ctrl-s equivalent for a never-saved project: ask WHERE, save THERE.
	After the save, re-probe the start() gates that depend on the folder
	(git presence, externalization need) so later steps see the real
	project. Returns True when the project is saved on disk."""
	try:
		if not _projectSaved():
			path=ui.chooseFile(load=False, fileTypes=['toe'],
				title='Save your project')
			if not path: return False
			project.save(path)
	except Exception:
		return False
	w=_w()
	try: w.store('git_missing', op.Embody.ext.Embody._findGitRootSync()=='no-git')
	except Exception: pass
	try: w.store('ext_needed', not op.Embody.ext.Embody._projectLooksExternalized())
	except Exception: pass
	# The Node Name fill waits for a saved project; now that it exists,
	# fill immediately so the parameter shows the real name this session.
	try: op.Convoy.ext.ConvoyExt._ensureNodeName()
	except Exception: pass
	return _projectSaved()
# How long the release gate waits before opening the dialog anyway. A
# CEILING, not the mechanism -- see _afterRelease.
_RELEASE_WAIT_FRAMES=120
def _pressed(o):
	"""True while `o` is under a held left mouse button.

	`lselect` is TouchDesigner's documented panel value for "left mouse
	button is pressed" (docs.derivative.ca/PanelValue_Class). Fails to
	False: a question we cannot answer must not be able to withhold the
	dialog forever."""
	try: return bool(o) and bool(o.panel.lselect)
	except Exception: return False
def _afterRelease(button, fn, waited=0):
	"""Run fn() once `button` is no longer held. Bounded.

	Never open a modal from inside the click callback: the OS dialog eats
	the mouse-UP, leaving the panel mid-press -- the two-clicks-to-continue
	bug. And wait on the actual release, not a frame count (a 2-frame defer
	lost to a normal 80-150ms press). _RELEASE_WAIT_FRAMES is only a
	ceiling so a button with no panel still gets its dialog."""
	if _pressed(button) and waited<_RELEASE_WAIT_FRAMES:
		run('args[0](args[1], args[2], args[3])', _afterRelease,
			button, fn, waited+1, delayFrames=1)
		return
	fn()
def _saveStepDeferred():
	"""The save step's dialog, run off the click edge -- see _afterRelease."""
	w=_w()
	try:
		if not w or w.fetch('step_id','')!='save':
			# Wizard closed/moved on while waiting for the release --
			# rendering now would overwrite _resetUI() with par values
			# the .tdn export ships in the release .tox.
			return
		done=_saveProjectNow()
		try:
			b=_grp('save').op('opt_savenow') if _grp('save') else None
			if b: b.par.value0 = 1 if done else 0
		except Exception: pass
		render()
	finally:
		# Cleared on EVERY path, including the cancel: leaving it set
		# would make the save card dead for the rest of the session.
		try:
			if w: w.unstore('save_pending')
		except Exception: pass

def _hintHeight(text):
	"""Panel height that FITS the hint, so every page's option group
	starts the same gap below the description. The old hardcoded 16/60
	split clipped long hints (Convoy) and pushed some pages' options
	lower than others (permissions)."""
	lines=0
	for para in str(text or '').split(chr(10)):
		lines+=max(1,-(-len(para)//62))
	return lines*15+6
def _convoyHint():
	# The save gate (step 1) guarantees a saved project by the time this
	# step shows; the parameter-page enable keeps its own
	# 'Waiting for project save' handling for non-wizard paths.
	return DEFS['convoy']['hint']
def _footprintHint():
	w=_w()
	if w.fetch('sel_assistant','')=='none' and w.fetch('sel_convoy','')=='enable':
		return 'Convoy-only setup adds a Python env (.venv + TDPyEnvManagerContext.yaml), the internal local Envoy command service, and .embody runtime state. It does not generate AI-client config or launch an AI coding tool. Enabling Convoy also installs and starts its per-user host app, consented in the Convoy step. Everything Embody adds is recorded and reversible via Uninstall.'
	return DEFS['footprint']['hint']
def render():
	w=_w(); sp=spine(); cur=w.fetch('step_id','mode')
	if cur not in sp: cur='mode'; w.store('step_id',cur)
	idx=sp.index(cur); d=DEFS[cur]
	w.op('steplabel').par.text='Step %d of %d'%(idx+1,len(sp))
	w.op('track/fill').par.w=int(452*(idx+1)/len(sp))
	for gid in GROUPS:
		g=w.op(gid)
		if g: g.par.display = 1 if gid==d.get('g') else 0
	w.op('title').par.text=d['title']
	w.op('hint').par.text=_saveHint() if cur=='save' else (_permHint() if cur=='permissions' else (_footprintHint() if cur=='footprint' else (_convoyHint() if cur=='convoy' else (_recap() if cur=='summary' else d['hint']))))
	w.op('hint').par.h = _hintHeight(w.op('hint').par.text.eval())
	bb=_nav('back'); nb=_nav('next'); first=idx==0
	if bb:
		if bb.op('text'): bb.op('text').par.text='Not now' if first else 'Back'
		_bg(bb, BACK_ON); _tc(bb.op('text'), TXT_DIM if first else TXT)
	last=cur=='summary'
	if cur=='save': ok=_projectSaved()  # the ONE gate Next cannot skip
	else: ok=last or (d.get('g') is None) or (_chosen(cur) is not None)
	if nb:
		if nb.op('text'): nb.op('text').par.text='Set up Embody' if last else 'Next'
		_bg(nb, NEXT_ON if ok else NEXT_OFF); _tc(nb.op('text'), NEXT_TXT if ok else TXT_DIM)
def click(name):
	w=_w(); sp=spine(); cur=w.fetch('step_id','mode')
	if name.startswith('btn_') or 'back' in name or 'next' in name:
		if 'back' in name:
			idx=sp.index(cur) if cur in sp else 0
			if idx>0: w.store('step_id', sp[idx-1]); render()
			else: _close()
		elif 'next' in name:
			d=DEFS[cur]
			if cur=='save' and not _projectSaved(): return
			if cur!='save' and d.get('g') and cur!='summary' and _chosen(cur) is None: return
			idx=sp.index(cur) if cur in sp else 0
			# finish() can open ui.chooseFolder (the custom-root footprint
			# choice), which is a modal from inside the click callback --
			# exactly what the save card had to stop doing.
			nb=_nav('next')
			if cur=='summary':
				# One-dialog-per-click guard (see opt_savenow): a click
				# queued behind chooseFolder would schedule a second
				# finish(). Cleared by start() on next open, not here.
				if w.fetch('finish_pending',False): return
				w.store('finish_pending',True)
				_afterRelease(nb, finish)
			elif idx<len(sp)-1: w.store('step_id', sp[idx+1]); render()
		return
	d=DEFS[cur]; g=_grp(cur)
	if cur=='save' and name=='opt_savenow':
		# Dialog opens on mouse-UP (see _afterRelease). One dialog per
		# click: a press queued behind the open dialog would re-open the
		# one the user just cancelled.
		if w.fetch('save_pending',False): return
		w.store('save_pending',True)
		_afterRelease(g.op('opt_savenow') if g else None, _saveStepDeferred)
		return
	if g:
		for c in g.children:
			if c.family=='COMP' and hasattr(c.par,'value0'): c.par.value0 = 1 if c.name==name else 0
	if d.get('sel'): w.store(d['sel'], name.replace('opt_',''))
	render()
def _resetUI():
	"""Restore the wizard's transient UI pars to their defaults.

	render() writes the step label, progress fill, title, hint and both
	button captions/colours straight onto COMPs INSIDE Embody. Those are
	ordinary parameters, so whatever the last wizard run left there is
	captured by Embody's own .tdn export, committed, and shipped in the
	release .tox -- the same leak the TDN export-progress dialog had.
	Resetting to par defaults means the wizard contributes nothing to the
	exported document. Best-effort: never block closing the window.
	"""
	def _def(o,*names):
		if not o: return
		for n in names:
			p=getattr(o.par,n,None)
			if p is not None:
				try: p.val=p.default
				except Exception: pass
	try:
		w=_w()
		if not w: return
		_def(w.op('steplabel'),'text')
		_def(w.op('track/fill'),'w')
		_def(w.op('title'),'text')
		_def(w.op('hint'),'text','h')
		for k in ('back','next'):
			b=_nav(k)
			if b:
				_def(b,'bgcolorr','bgcolorg','bgcolorb')
				_def(b.op('text'),'text','fontcolorr','fontcolorg',
					 'fontcolorb','textcolorr','textcolorg','textcolorb')
		for gid in GROUPS:
			_def(w.op(gid),'display')
	except Exception: pass
def _close():
	_resetUI()
	try: op.Embody.op('window_wizard').par.winclose.pulse()
	except Exception: pass
def finish():
	w=_w()
	m=w.fetch('sel_mode','auto'); a=w.fetch('sel_assistant','claudecode')
	c=w.fetch('sel_client',''); r=w.fetch('sel_root','gitroot')
	pm=w.fetch('sel_permissions','all')
	g=w.fetch('sel_git','') if w.fetch('git_missing',False) else ''
	# Only pass a choice the user was actually shown -- same test spine()
	# uses, so a hidden step can never mutate the whole project.
	x=w.fetch('sel_externalize','') if 'externalize' in spine() else ''
	cv=w.fetch('sel_convoy','') if 'convoy' in spine() else ''
	cr=''
	if r=='custom':
		try: cr=ui.chooseFolder(title='Choose the folder for Embody config') or ''
		except Exception: cr=''
	_close()
	# Defer so the window closes cleanly before setup runs. Tokens come from the
	# wizard's fixed option set, so repr-interpolation is safe.
	run('op.Embody.ext.Embody._applyWizardSetup(mode=%r, assistant=%r, client=%r, root=%r, custom_root=%r, permissions=%r, git=%r, externalize=%r, convoy=%r)'%(m,a,c,r,cr,pm,g,x,cv), delayFrames=2)
def start():
	w=_w()
	# A wizard closed mid-dialog would otherwise reopen with its save
	# card (or its Set up Embody button) permanently refusing to fire.
	for _flag in ('save_pending','finish_pending'):
		try: w.unstore(_flag)
		except Exception: pass
	w.store('needs_save', not _projectSaved())
	w.store('step_id','save' if w.fetch('needs_save',False) else 'mode')
	for gid in GROUPS:
		g=w.op(gid)
		if g:
			for c in g.children:
				if c.family=='COMP' and hasattr(c.par,'value0'): c.par.value0=0
	md=op.Embody.par.Embodymode.eval()
	mb=w.op('grp_mode/opt_'+md)
	if mb: mb.par.value0=1
	w.store('sel_mode',md)
	try: ai=str(op.Embody.par.Aiclient.eval() or 'claudecode')
	except Exception: ai='claudecode'
	a=('none' if ai=='none' else ('claudecode' if ai=='claudecode' else 'other'))
	ab=w.op('grp_assistant/opt_'+a)
	if ab: ab.par.value0=1
	if a=='other':
		cb=w.op('grp_client/opt_'+ai)
		if cb: cb.par.value0=1
	pb=w.op('grp_permissions/opt_all')
	if pb: pb.par.value0=1
	# Git step gate: same probe the enable path uses (_findGitRootSync), so
	# the wizard and Start() can never disagree about "no repo".
	try: missing=(op.Embody.ext.Embody._findGitRootSync()=='no-git')
	except Exception: missing=False
	w.store('git_missing',missing)
	gb=w.op('grp_git/opt_gitinit')
	if gb: gb.par.value0=1
	# Externalize step gate: probe ONCE here (spine() runs on every render and
	# must stay cheap + unraisable). Any doubt shows the step -- a needless
	# click beats silently withholding the offer.
	try: need=not op.Embody.ext.Embody._projectLooksExternalized()
	except Exception: need=True
	w.store('ext_needed',need)
	# Pre-select the safe, non-destructive option: externalize new work only.
	xb=w.op('grp_externalize/opt_auto')
	if xb: xb.par.value0=1
	# Convoy step: pre-select the CURRENT enable state so re-running the wizard
	# never silently flips an existing choice (as mode/git are pre-selected).
	try: cv_on=bool(op.Embody.par.Convoyenable.eval())
	except Exception: cv_on=False
	vb=w.op('grp_convoy/opt_'+('enable' if cv_on else 'disable'))
	if vb: vb.par.value0=1
	w.store('sel_assistant',a); w.store('sel_client',ai if a=='other' else ''); w.store('sel_root','gitroot'); w.store('sel_permissions','all'); w.store('sel_git','gitinit'); w.store('sel_externalize','auto'); w.store('sel_convoy','enable' if cv_on else 'disable')
	render()
