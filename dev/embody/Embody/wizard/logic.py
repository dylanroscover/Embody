SURF=(0.16,0.17,0.165)
BACK_ON=(0.19,0.20,0.195); NEXT_ON=(0.24,0.52,0.35); NEXT_OFF=(0.135,0.15,0.14)
TXT=(0.92,0.92,0.92); TXT_DIM=(0.34,0.35,0.34); NEXT_TXT=(0.97,0.99,0.97)
GROUPS=['grp_mode','grp_assistant','grp_client','grp_permissions','grp_footprint']
DEFS={
 'mode':{'g':'grp_mode','sel':'sel_mode','title':'How should Embody manage your project?','hint':'Choose one, then Next.'},
 'assistant':{'g':'grp_assistant','sel':'sel_assistant','title':'Turn on the AI assistant (Envoy)?','hint':'It lets AI tools work in your network. Easy to remove later.'},
 'client':{'g':'grp_client','sel':'sel_client','title':'Pick your AI coding tool','hint':'Embody will generate its config.'},
 'footprint':{'g':'grp_footprint','sel':'sel_root','title':'Review what Embody will add','hint':'Embody will add a Python env (.venv) + MCP server, config files (.mcp.json, .embody, AI rules), .gitignore/.gitattributes entries + a .tdn diff driver, and the Embot assistant in your network. Everything is recorded and reversible via Uninstall.'},
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
	s=['mode','assistant']
	if a=='other': s.append('client')
	if a=='claudecode': s.append('permissions')
	if m=='advanced' and a not in (None,'none'): s.append('footprint')
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
	if a=='none': return 'Mode: %s. AI assistant: off (externalization only).\nNothing has changed yet - click Set up Embody to apply.'%m
	c=w.fetch('sel_client','') if a=='other' else ('Claude Code' if a=='claudecode' else a)
	return 'Mode: %s. AI assistant: on (%s).\nNothing has changed yet - click Set up Embody to apply.'%(m, c or 'your tool')
def _permHint():
	base=DEFS['permissions']['hint']
	try:
		from pathlib import Path
		root=op.Embody.ext.Embody._findProjectRoot()
		if root and (Path(str(root))/'.claude'/'settings.local.json').exists():
			base+='\nA settings.local.json already exists -- Embody edits only its Envoy entries, keeping the rest.'
	except Exception: pass
	return base
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
	w.op('hint').par.text=_permHint() if cur=='permissions' else (_recap() if cur=='summary' else d['hint'])
	w.op('hint').par.h = 60 if cur in ('footprint','summary','permissions') else 16
	bb=_nav('back'); nb=_nav('next'); first=idx==0
	if bb:
		if bb.op('text'): bb.op('text').par.text='Not now' if first else 'Back'
		_bg(bb, BACK_ON); _tc(bb.op('text'), TXT_DIM if first else TXT)
	last=cur=='summary'; ok=last or (d.get('g') is None) or (_chosen(cur) is not None)
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
			if d.get('g') and cur!='summary' and _chosen(cur) is None: return
			idx=sp.index(cur) if cur in sp else 0
			if cur=='summary': finish()
			elif idx<len(sp)-1: w.store('step_id', sp[idx+1]); render()
		return
	d=DEFS[cur]; g=_grp(cur)
	if g:
		for c in g.children:
			if c.family=='COMP' and hasattr(c.par,'value0'): c.par.value0 = 1 if c.name==name else 0
	if d.get('sel'): w.store(d['sel'], name.replace('opt_',''))
	render()
def _close():
	try: op.Embody.op('window_wizard').par.winclose.pulse()
	except Exception: pass
def finish():
	w=_w()
	m=w.fetch('sel_mode','auto'); a=w.fetch('sel_assistant','claudecode')
	c=w.fetch('sel_client',''); r=w.fetch('sel_root','gitroot')
	pm=w.fetch('sel_permissions','all')
	cr=''
	if r=='custom':
		try: cr=ui.chooseFolder(title='Choose the folder for Embody config') or ''
		except Exception: cr=''
	_close()
	# Defer so the window closes cleanly before setup runs. Tokens come from the
	# wizard's fixed option set, so repr-interpolation is safe.
	run('op.Embody.ext.Embody._applyWizardSetup(mode=%r, assistant=%r, client=%r, root=%r, custom_root=%r, permissions=%r)'%(m,a,c,r,cr,pm), delayFrames=2)
def start():
	w=_w(); w.store('step_id','mode')
	for gid in GROUPS:
		g=w.op(gid)
		if g:
			for c in g.children:
				if c.family=='COMP' and hasattr(c.par,'value0'): c.par.value0=0
	md=op.Embody.par.Embodymode.eval()
	mb=w.op('grp_mode/opt_'+md)
	if mb: mb.par.value0=1
	w.store('sel_mode',md)
	ab=w.op('grp_assistant/opt_claudecode')
	if ab: ab.par.value0=1
	pb=w.op('grp_permissions/opt_all')
	if pb: pb.par.value0=1
	w.store('sel_assistant','claudecode'); w.store('sel_client',''); w.store('sel_root','gitroot'); w.store('sel_permissions','all')
	render()
