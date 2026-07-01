"""
Envoy toggle-on crash bisect, v2 (dev-only diagnostic).

v1 narrowed the crash to _configureMCPClient(9870). This v2 reproduces
the body of that method step by step, with each sub-step's progress
flushed to disk BEFORE it runs, so the file's last `about_to_run` entry
names the exact operation that killed TD.

Strongest suspect: the subprocess.run that probes the venv Python
([EnvoyExt._configureMCPClient line 4302]) -- TD on Windows already has
one documented `DuplicateHandle` landmine on stdin (the existing
`stdin=subprocess.DEVNULL` workaround), and 2025.31550 reworked the
Windows console / subprocess-output capture, plausibly creating a
sibling issue on stdout/stderr that bites the SECOND call in a session.

HOW TO USE
----------
1. Open the crashing project. Toggle Envoyenable OFF, wait for status
   "Disabled".
2. Create a textDAT, paste this entire script, Right-click -> Run Script.
3. If TD closes, reopen the project.
4. Send back the file at:
       <project.folder>/.embody/crash-bisect-v2.json

WHAT TO LOOK FOR
----------------
The last `about_to_run` entry names the killer sub-step. The most
informative ones to watch for:

  subprocess_run_venv_python   -> Windows subprocess handle issue in 32820
  mkdir_bridge_dir              -> filesystem (very unlikely)
  read_existing_bridge          -> file read on D:\ with a space in path
  write_bridge                  -> file write
  writeEnvoyConfig              -> JSON write to envoy.json
  write_mcp_json                -> JSON write to .mcp.json
  deploySettingsLocal           -> settings.local.json write
"""

import os
import sys
import json
import subprocess
import traceback
from datetime import datetime
from pathlib import Path


# --- Output -----------------------------------------------------------------

_proj = project.folder
_out = os.path.join(_proj, '.embody', 'crash-bisect-v2.json')
try:
	os.makedirs(os.path.dirname(_out), exist_ok=True)
except OSError:
	_out = os.path.join(_proj, 'crash-bisect-v2.json')

_events = []
_meta = {}


def _flush():
	payload = {'meta': _meta, 'events': _events}
	with open(_out, 'w', encoding='utf-8') as f:
		json.dump(payload, f, indent=2)
		f.flush()
		try:
			os.fsync(f.fileno())
		except OSError:
			pass


def _record(step, phase, info=''):
	_events.append({
		'step': step,
		'phase': phase,
		'time': datetime.now().isoformat(timespec='seconds'),
		'info': str(info)[:1000],
	})
	_flush()


def _safe(fn):
	try:
		return fn()
	except Exception as e:
		return f'<error: {e}>'


_meta = {
	'td_version': _safe(lambda: f'{app.version}.{app.build}'),
	'embody_version': _safe(lambda: op.Embody.par.Version.eval()),
	'os': sys.platform,
	'project_name': _safe(lambda: project.name),
	'project_folder': _proj,
	'envoyenable_par': _safe(lambda: bool(op.Embody.par.Envoyenable.eval())),
	'envoystatus_par': _safe(lambda: str(op.Embody.par.Envoystatus.eval())),
	'output_path': _out,
}

_record('preflight', 'snapshot', json.dumps(_meta))


# --- Reproduce _configureMCPClient(port=9870) body, step by step ----------

port = 9870
envoy_ext = op.Embody.ext.Envoy


def step(name, fn):
	"""Run a sub-step with before/after disk records."""
	_record(name, 'about_to_run')
	try:
		r = fn()
		_record(name, 'completed', repr(r)[:500])
		return r
	except Exception:
		_record(name, 'exception', traceback.format_exc())
		raise


try:
	# --- Path setup ---
	project_dir = step('compute_project_dir', lambda: Path(project.folder))

	target_dir = step('compute_target_dir',
		lambda: project_dir)  # no git in his project, so target_dir = project_dir

	bridge_dir = step('compute_bridge_dir', lambda: target_dir / '.embody')

	step('mkdir_bridge_dir',
		lambda: bridge_dir.mkdir(parents=True, exist_ok=True))

	bridge_path = step('compute_bridge_path',
		lambda: bridge_dir / 'envoy-bridge.py')

	# --- Read bridge content from templates DAT ---
	def _read_template():
		templates = op.Embody.op('templates')
		bridge_dat = templates.op('text_envoy_bridge') if templates else None
		return bridge_dat.text if bridge_dat else None

	bridge_content = step('read_bridge_template_dat', _read_template)

	if not bridge_content:
		# Fallback: read from dev/embody/ source
		source = step('compute_fallback_bridge_source',
			lambda: Path(project.folder) / 'embody' / 'envoy_bridge.py')
		if source.exists():
			bridge_content = step('read_fallback_bridge_source',
				lambda: source.read_text(encoding='utf-8'))

	step('bridge_content_length',
		lambda: f'{len(bridge_content) if bridge_content else 0} chars')

	# --- Compare with existing bridge on disk ---
	exists = step('bridge_path_exists', lambda: bridge_path.exists())

	if exists:
		existing = step('read_existing_bridge',
			lambda: bridge_path.read_text(encoding='utf-8'))
		needs_write = step('compare_bridge_content',
			lambda: existing != bridge_content)
	else:
		needs_write = True
		step('compare_bridge_content', lambda: 'skipped (no existing file)')

	# --- Write bridge if different ---
	if needs_write:
		step('write_bridge',
			lambda: bridge_path.write_text(bridge_content, encoding='utf-8'))
	else:
		step('write_bridge', lambda: 'skipped (content unchanged)')

	# --- Migration unlinks (no-ops if files don't exist) ---
	old_bridge = step('compute_old_bridge_path',
		lambda: target_dir / '.claude' / 'envoy-bridge.py')
	step('migrate_old_bridge',
		lambda: old_bridge.unlink() if old_bridge.exists() else 'absent')

	for _old_name in ('.envoy-tools-cache.json', '.envoy.json', '.embody.json'):
		step(f'migrate_old_{_old_name}',
			lambda n=_old_name: (
				(target_dir / n).unlink()
				if (target_dir / n).exists() else 'absent'))

	# --- Compute venv Python path ---
	if sys.platform == 'win32':
		venv_python = step('compute_venv_python_win',
			lambda: project_dir / '.venv' / 'Scripts' / 'python.exe')
	else:
		venv_python = step('compute_venv_python_posix',
			lambda: project_dir / '.venv' / 'bin' / 'python3')

	is_file = step('venv_python_is_file', lambda: venv_python.is_file())

	# --- THE PRIME SUSPECT: subprocess.run against venv python ---
	if is_file:
		def _subprocess_call():
			return subprocess.run(
				[str(venv_python), '-c', 'import sys; print(sys.version)'],
				capture_output=True, timeout=10, check=True,
				stdin=subprocess.DEVNULL,
			).stdout.decode('utf-8', 'replace')[:200]

		step('subprocess_run_venv_python', _subprocess_call)
		python_cmd = str(venv_python).replace('\\', '/')
	else:
		python_cmd = 'python' if sys.platform == 'win32' else 'python3'

	step('python_cmd_chosen', lambda: python_cmd)

	# --- Write envoy.json (the project config the bridge reads) ---
	step('writeEnvoyConfig',
		lambda: envoy_ext._writeEnvoyConfig(target_dir / '.embody', port))

	# --- Write .mcp.json (the MCP client config) ---
	mcp_file = step('compute_mcp_file_path', lambda: target_dir / '.mcp.json')
	bridge_abs = str(bridge_path).replace('\\', '/')
	config_abs = str(
		(target_dir / '.embody' / 'envoy.json')).replace('\\', '/')

	existing_mcp = {}
	if mcp_file.exists():
		def _read_mcp():
			try:
				return json.loads(mcp_file.read_text(encoding='utf-8'))
			except (json.JSONDecodeError, OSError):
				return {}
		existing_mcp = step('read_existing_mcp_json', _read_mcp)
	else:
		step('read_existing_mcp_json', lambda: 'absent')

	servers = existing_mcp.get('mcpServers', {}) if isinstance(existing_mcp, dict) else {}
	expected_args = ['-u', bridge_abs, '--port', str(port),
		'--config', config_abs]
	existing = servers.get('envoy', {})

	already_configured = (
		existing.get('type') == 'stdio'
		and existing.get('command') == python_cmd
		and existing.get('args') == expected_args)

	step('mcp_already_configured', lambda: already_configured)

	if not already_configured:
		def _write_mcp():
			servers['envoy'] = {
				'type': 'stdio',
				'command': python_cmd,
				'args': expected_args,
			}
			existing_mcp['mcpServers'] = servers
			mcp_file.write_text(
				json.dumps(existing_mcp, indent=2) + '\n', encoding='utf-8')
			return 'written'
		step('write_mcp_json', _write_mcp)
	else:
		step('write_mcp_json', lambda: 'skipped (already configured)')

	# --- Deploy settings.local.json (auto-allow read-only MCP tools) ---
	step('deploySettingsLocal',
		lambda: envoy_ext._deploySettingsLocal(target_dir / '.claude'))

	_record('all_sub_steps', 'completed',
		'every sub-step of _configureMCPClient survived in isolation')

except Exception:
	# Recorded in step()'s except; we just stop here.
	pass


# --- Async tail: confirm TD is still alive 5 seconds later ----------------

def _alive_check():
	try:
		_record('alive_after_5s', 'survived',
			f'envoystatus={op.Embody.par.Envoystatus.eval()!r}')
	except Exception:
		_record('alive_after_5s', 'exception', traceback.format_exc())


run('args[0]()', _alive_check, delayFrames=300)

print(f'[diagnose_envoy_toggle_crash_v2] events written to: {_out}')
