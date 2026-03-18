# Text Component callbacks

def onValueChange(comp, value, prevValue):
	"""
	Called whenever the text component's value parameter changes for any reason including typing
	in the text window or changing the parameter directly. The callback is equivalent to the
	onValueChange in the parameter exectute DAT.
	
	Args:
		comp: the text component
		value: the new value
		prevValue: the previous value
	"""
	#debug('\nonValueChange comp:', comp.path, '- new value: ', value, ', prev value: ', prevValue )
	return
	
def onFocus(comp):
	"""
	Called when one of the text components viewers gets keyboard focus. 
	
	Args:
		comp: the text component
	"""
	#debug('\nonFocus comp:', comp.path)
	return

def onFocusEnd(comp, info):
	"""
	Called when one of the text component's viewers loses keyboard focus. In regular editing mode
	this is right after the contents are saved back to the value parameter. This function is called
	everytime the viewer loses focus, even if the text does not change.
	
	Args:
		comp: the text component
		info: a dictionary containing information about the event including:
			reason: a string indicating what caused the viewer to lose focus e.g. 'enter', 'escape', 'tab', 'unknown'
	
	"""
	#debug('\nonFocusEnd comp:', comp.path, '- info:\n', info)
	return
	
def onTextEdit(comp):
	"""
	Called each time the contents of the text component viewer change i.e. when typing a character,
	deleting text, cutting or pasting. 
	
	Args:
		comp: the text component
	"""
	#debug('\nonTextEdit comp:', comp.path, '- text: ', comp.editText)
	return
	
def onTextEditEnd(comp, value, prevValue):
	"""
	Called when the user finishes editing and the value has been changed. Not called in 'Continuous Edit' mode. Use
	onTextEdit and onFocusEnd when using continuous editing.
	
	Args:
		comp: the text component
		value: the new value
		prevValue: the previous value
	"""
	#debug('\nonTextEditEnd comp:', comp.path, '- new value: ', value, ', prev value: ', prevValue )
	return
	
	