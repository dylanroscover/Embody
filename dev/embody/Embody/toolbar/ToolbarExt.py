"""Toolbar extension: centralized click dispatch, tooltip management, filter handling."""


class ToolbarExt:

	def __init__(self, ownerComp):
		self.ownerComp = ownerComp
		self._lastHovered = None
		self._lastPressed = None
		self._ensureMeasureTOP()

	def _ensureMeasureTOP(self):
		"""Ensure tip_measure textTOP exists with correct font settings."""
		measure = self.ownerComp.op('tip_measure')
		if not measure:
			measure = self.ownerComp.create(textTOP, 'tip_measure')
			measure.nodeX = 1400
			measure.nodeY = -200
		measure.par.font = 'Open Sans'
		measure.par.fontsizex = 10
		measure.par.fontsizey = 10

	@property
	def embody(self):
		return self.ownerComp.parent.Embody

	# ── Click dispatch ──────────────────────────────────────────────

	def OnContainerClick(self, container):
		"""Called by panelexec_left/right on lselect offToOn."""
		btn = self._findClickedButton(container)
		if not btn:
			return

		config = self.ownerComp.op('toolbar_config')
		name = btn.name
		try:
			action = config[name, 'action'].val
		except:
			return

		if not action or action == '-':
			return

		handler = getattr(self, '_action_' + action, None)
		if handler:
			handler()
			return

		try:
			method = getattr(self.ownerComp.parent.Embody, action, None)
			if method and callable(method):
				method()
		except Exception as e:
			debug(f'ToolbarExt error calling {action}: {e}')

	# ── Press / Release ─────────────────────────────────────────────

	def OnContainerPress(self, container):
		"""Called by panelexec on lselect offToOn — set pressed visual."""
		btn = self._findClickedButton(container)
		if btn:
			self._setPressed(btn)

	def OnContainerRelease(self, container):
		"""Called by panelexec on lselect onToOff — clear pressed visual."""
		self._clearPressed()

	def _setPressed(self, btn):
		"""Set pressed state on a button via store(), clear previous."""
		if self._lastPressed and self._lastPressed.valid and self._lastPressed != btn:
			self._lastPressed.store('pressed', False)
		btn.store('pressed', True)
		btn.store('hover', False)
		self._lastPressed = btn

	def _clearPressed(self, restore_hover=True):
		"""Clear pressed state from last pressed button."""
		if self._lastPressed and self._lastPressed.valid:
			self._lastPressed.store('pressed', False)
			if restore_hover:
				self._lastPressed.store('hover', True)
		self._lastPressed = None

	# ── Rollover / Hover ────────────────────────────────────────────

	def OnContainerRollover(self, container, state):
		"""Called by panelexec on rollover/insideu valueChange."""
		if state:
			btn = self._findButtonByPosition(container)
			if btn:
				self._setHover(btn)
				config = self.ownerComp.op('toolbar_config')
				try:
					tooltip_text = config[btn.name, 'tooltip'].val
				except:
					tooltip_text = ''
				if tooltip_text and tooltip_text != '-':
					self._showTooltip(btn, tooltip_text, container)
				else:
					self._hideTooltip()
				return
		self._clearHover()
		self._hideTooltip()

	def _setHover(self, btn):
		"""Set hover state on a button via store(), clear previous."""
		if self._lastHovered and self._lastHovered.valid and self._lastHovered != btn:
			self._lastHovered.store('hover', False)
		btn.store('hover', True)
		self._lastHovered = btn

	def _clearHover(self):
		"""Clear hover state from last hovered button."""
		if self._lastHovered and self._lastHovered.valid:
			self._lastHovered.store('hover', False)
		self._lastHovered = None
		self._clearPressed(restore_hover=False)

	# ── Tooltip ──────────────────────────────────────────────────────

	def _measureTextWidth(self, text):
		"""Measure text pixel width using a textTOP with matching font."""
		measure = self.ownerComp.op('tip_measure')
		if not measure:
			self._ensureMeasureTOP()
			measure = self.ownerComp.op('tip_measure')
		measure.par.text = text
		measure.cook(force=True)
		return measure.textWidth

	def _showTooltip(self, button_op, text, container=None):
		tip = self.ownerComp.op('tooltip')
		if not tip:
			return
		tip_text = tip.op('text')
		if not tip_text:
			return
		tip_text.par.text = text

		text_width = self._measureTextWidth(text)
		tip_w = int(text_width + 20)
		tip.par.w = tip_w

		if container is None:
			container = button_op.parent()

		# Use .width/.height for actual rendered size (par.w is stale in fill mode)
		container_x = self._getRenderedX(container)
		btn_w = button_op.width
		toolbar_w = self.ownerComp.width

		btn_x = self._getAlignX(button_op)
		btn_center_x = container_x + btn_x + btn_w / 2.0
		tip_x = btn_center_x - tip_w / 2.0
		tip_x = max(2, min(tip_x, toolbar_w - tip_w - 2))

		tip.par.x = int(tip_x)
		tip.par.y = int(self.ownerComp.height)
		tip.par.display = True

	def _hideTooltip(self):
		tip = self.ownerComp.op('tooltip')
		if tip:
			tip.par.display = False

	def _getRenderedX(self, comp):
		"""Compute actual rendered X from anchor/origin layout parameters."""
		if comp == self.ownerComp:
			return 0
		parent_w = self.ownerComp.width
		anchor = comp.par.leftanchor.eval()
		offset = comp.par.x.eval()
		origin = comp.par.horigin.eval()
		comp_w = comp.width
		return anchor * parent_w + offset - origin * comp_w

	# ── Filter handling ─────────────────────────────────────────────

	def OnFilterChanged(self):
		"""Called when filter text changes. Refresh the externalization list."""
		self.ownerComp.parent.Embody.op('list/inject_parents').cook(force=True)
		self.ownerComp.parent.Embody.op('list/list1').par.reset.pulse()

	# ── Action handlers ─────────────────────────────────────────────

	def _action_toggle_disable(self):
		emb = self.ownerComp.parent.Embody
		if emb.par.Status.eval() == 'Enabled':
			emb.DisableHandler()
		else:
			emb.UpdateHandler()

	def _action_toggle_envoy(self):
		emb = self.ownerComp.parent.Embody
		current = emb.par.Envoyenable.eval()
		emb.par.Envoyenable = not current

	def _action_export_tdn(self):
		self.ownerComp.parent.Embody.ext.TDN.ExportProjectTDNInteractive()

	def _action_export_comp_tdn(self):
		comp = None
		for pane in ui.panes:
			if pane.type == PaneType.NETWORKEDITOR:
				comp = pane.owner
				break
		if comp:
			self.ownerComp.parent.Embody.ext.TDN.ExportNetworkAsync(
				root_path=comp.path, output_file='auto')

	def _action_clear_filter(self):
		f = self.ownerComp.op('container_right/filter')
		if f:
			f.par.text = ''
			self.OnFilterChanged()

	# ── Helpers ─────────────────────────────────────────────────────

	def _findClickedButton(self, container):
		"""Find clicked button using lradioname, fallback to position."""
		try:
			name = container.panel.lradioname.val
			if name:
				btn = container.op(name)
				if btn:
					return btn
		except:
			pass
		return self._findButtonByPosition(container)

	def _findButtonByPosition(self, container):
		"""Find button at mouse position using insideu and cumulative align."""
		try:
			u = container.panel.insideu.val
		except:
			return None
		container_w = container.width
		pixel_x = u * container_w
		children = self._getAlignedChildren(container)
		align = container.par.align.eval()

		if align == 'horizrl':
			cumulative_x = container_w
			for child in children:
				w = child.width
				left_edge = cumulative_x - w
				if left_edge <= pixel_x <= cumulative_x:
					config = self.ownerComp.op('toolbar_config')
					try:
						_ = config[child.name, 'name']
						return child
					except:
						cumulative_x = left_edge
						continue
				cumulative_x = left_edge
		else:
			cumulative_x = 0
			for child in children:
				w = child.width
				if cumulative_x <= pixel_x <= cumulative_x + w:
					config = self.ownerComp.op('toolbar_config')
					try:
						_ = config[child.name, 'name']
						return child
					except:
						cumulative_x += w
						continue
				cumulative_x += w
		return None

	def _getAlignX(self, button_op):
		"""Get a button's X position from cumulative align order."""
		container = button_op.parent()
		children = self._getAlignedChildren(container)
		align = container.par.align.eval()
		container_w = container.width

		if align == 'horizrl':
			cumulative_x = container_w
			for child in children:
				w = child.width
				cumulative_x -= w
				if child == button_op:
					return cumulative_x
		else:
			cumulative_x = 0
			for child in children:
				if child == button_op:
					return cumulative_x
				cumulative_x += child.width
		return 0

	def _getAlignedChildren(self, container):
		"""Get visible COMP children sorted by alignorder."""
		children = []
		for c in container.children:
			if c.family != 'COMP':
				continue
			if not hasattr(c.par, 'display') or not c.par.display.eval():
				continue
			children.append(c)
		children.sort(key=lambda c: c.par.alignorder.eval())
		return children
