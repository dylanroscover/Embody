"""Window header extension: title label + minimize/maximize/close buttons."""


class WindowHeaderExt:

	def __init__(self, ownerComp):
		self.ownerComp = ownerComp
		self._lastHovered = None
		self._lastPressed = None

		self.min_width = 410
		self.min_height = 66
		self.max_width = 1080
		self.max_height = 440
		self.status_compact_w = 80
		self.status_default_w = 80

		# Toolbar buttons hidden in compact mode (envoy_toggle stays visible)
		self._compact_hide_names = (
			'save_folder',
			'envoy_status', 'disable',
		)

		# Known-good default display states for maximize restore
		self._defaults = {
			'status':           {'mode': 'constant', 'val': 0},
			'disable':          {'mode': 'constant', 'val': 1},
			'initialize':       {'mode': 'constant', 'val': 1},
			'refresh':          {'mode': 'expression',
				'expr': "1 if parent.Embody.par.Status == 'Enabled' else 0"},
			'save_comp':        {'mode': 'constant', 'val': 1},
			'save_folder':      {'mode': 'expression',
				'expr': "1 if parent.Embody.par.Status == 'Enabled' else 0"},
			'export_comp_tdn':  {'mode': 'constant', 'val': 1},
			'export_tdn':       {'mode': 'constant', 'val': 1},
			'import_tdn':       {'mode': 'constant', 'val': 1},
			'envoy_status':     {'mode': 'constant', 'val': 1},
			'envoy_toggle':     {'mode': 'constant', 'val': 1},
		}

	@property
	def _mgr(self):
		return self.ownerComp.parent.Embody

	@property
	def _tools(self):
		return self._mgr.op('toolbar')

	@property
	def _lister(self):
		return self._mgr.op('list')

	@property
	def _container_left(self):
		return self._tools.op('container_left') if self._tools else None

	@property
	def _filter(self):
		return self._tools.op('container_right') if self._tools else None

	# ── Press / Release ─────────────────────────────────────────────

	def OnPress(self):
		"""Called by panelexec1 on lselect offToOn -- set pressed visual."""
		btn = self._findClickedButton()
		if btn:
			self._setPressed(btn)

	def OnRelease(self):
		"""Called by panelexec1 on lselect onToOff -- clear pressed visual."""
		self._clearPressed()

	def _setPressed(self, btn):
		if self._lastPressed and self._lastPressed.valid and self._lastPressed != btn:
			self._lastPressed.store('pressed', False)
		btn.store('pressed', True)
		btn.store('hover', False)
		self._lastPressed = btn

	def _clearPressed(self, restore_hover=True):
		if self._lastPressed and self._lastPressed.valid:
			self._lastPressed.store('pressed', False)
			if restore_hover:
				self._lastPressed.store('hover', True)
		self._lastPressed = None

	# ── Click dispatch ──────────────────────────────────────────────

	def OnClick(self):
		"""Called by panelexec1 on lselect offToOn."""
		btn = self._findClickedButton()
		if not btn:
			return
		actions = {
			'button_min': self.Windowminimize,
			'button_max': self.Windowmaximize,
			'button_close': self.Windowclose,
		}
		action = actions.get(btn.name)
		if action:
			action()

	# ── Rollover ────────────────────────────────────────────────────

	def OnRollover(self, state):
		"""Called by panelexec1 on rollover/insideu changes."""
		if state:
			btn = self._findButtonByPosition()
			if btn:
				self._setHover(btn)
				return
		self._clearHover()

	def _setHover(self, btn):
		if self._lastHovered and self._lastHovered.valid and self._lastHovered != btn:
			self._lastHovered.store('hover', False)
		btn.store('hover', True)
		self._lastHovered = btn

	def _clearHover(self):
		if self._lastHovered and self._lastHovered.valid:
			self._lastHovered.store('hover', False)
		self._lastHovered = None
		self._clearPressed(restore_hover=False)

	# ── Window actions ──────────────────────────────────────────────

	def Windowclose(self):
		"""Close the manager window."""
		windowComp = self.ownerComp.par.Windowcomp.eval()
		if isinstance(windowComp, windowCOMP):
			windowComp.par.winclose.pulse()

	def Windowmaximize(self):
		"""Restore to full manager view."""
		container_left = self._container_left
		if container_left:
			for name, dflt in self._defaults.items():
				btn = container_left.op(name)
				if not btn:
					continue
				if dflt['mode'] == 'expression':
					btn.par.display.mode = ParMode.EXPRESSION
					btn.par.display.expr = dflt['expr']
				else:
					btn.par.display = dflt['val']
			status = container_left.op('status')
			if status:
				status.par.w = self.status_default_w

		tools = self._tools
		lister = self._lister
		filt = self._filter
		mgr = self._mgr

		if tools:
			tools.par.display = 1
		if lister:
			lister.par.display = 1
		if filt:
			filt.par.display = 1

		mgr.par.w = self.max_width
		run("args[0].par.h = args[1]", mgr, self.max_height, delayFrames=1)
		self._mgr.Refresh()

	def Windowminimize(self):
		"""Collapse to compact widget mode."""
		container_left = self._container_left
		if container_left:
			for name in self._compact_hide_names:
				btn = container_left.op(name)
				if btn:
					btn.par.display = 0
			status = container_left.op('status')
			if status:
				status.par.display = 1
				status.par.w = self.status_compact_w

		lister = self._lister
		filt = self._filter
		mgr = self._mgr

		if lister:
			lister.par.display = 0
		if filt:
			filt.par.display = 0

		mgr.par.w = self.min_width
		run("args[0].par.h = args[1]", mgr, self.min_height, delayFrames=1)

	# ── Helpers ─────────────────────────────────────────────────────

	def _findClickedButton(self):
		"""Find clicked button using insideu position."""
		try:
			name = self.ownerComp.panel.lradioname.val
			if name:
				btn = self.ownerComp.op(name)
				if btn and btn.name.startswith('button_'):
					return btn
		except:
			pass
		return self._findButtonByPosition()

	def _findButtonByPosition(self):
		"""Find button at mouse position using insideu and cumulative align."""
		try:
			u = self.ownerComp.panel.insideu.val
		except:
			return None
		container_w = self.ownerComp.width
		pixel_x = u * container_w
		children = self._getAlignedChildren()

		cumulative_x = 0
		for child in children:
			w = child.width
			if cumulative_x <= pixel_x <= cumulative_x + w:
				if child.name.startswith('button_'):
					return child
			cumulative_x += w
		return None

	def _getAlignedChildren(self):
		"""Get visible COMP children sorted by alignorder."""
		children = []
		for c in self.ownerComp.children:
			if c.family != 'COMP':
				continue
			if not hasattr(c.par, 'display') or not c.par.display.eval():
				continue
			children.append(c)
		children.sort(key=lambda c: c.par.alignorder.eval())
		return children
