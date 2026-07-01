"""
Envoy toggle-on crash bisect (dev-only diagnostic).

The bug: on some users, toggling Envoyenable OFF then back ON closes
TouchDesigner with no Python traceback. Repros on their project, not on
a fresh one.

This script bisects EnvoyExt.Start() step by step on the live, crashing
project. Each step's progress is flushed to disk BEFORE the step runs --
if TD closes during a step, the file's last `about_to_run` entry names
the step that killed it.

HOW TO USE
----------
1. On the affected user's machine, open the crashing project.
2. Toggle the Embody COMP's Envoyenable parameter OFF. Wait for the
   Envoystatus parameter to read "Disabled".
3. Create a textDAT anywhere in the network. Paste the entire body of
   this script into it. Right-click the DAT -> Run Script.
4. If TD closes during the run, reopen the project. (The script wrote
   progress to disk before each step, so the diagnosis is intact.)
5. Send back the file at:
       <project.folder>/.embody/crash-bisect.json
   (or, if .embody/ couldn't be created, <project.folder>/crash-bisect.json)

WHAT TO LOOK FOR IN THE OUTPUT
------------------------------
- File ends at `step: <X>, phase: about_to_run` -> step <X> killed TD.
- File ends at `step: full_Start, phase: returned_synchronously` with NO
  later `alive_after_5s` entry -> Start() returned, but the worker
  thread + uvicorn killed TD asynchronously a few frames later.
- File ends with `step: alive_after_5s, phase: survived` -> the script
  did NOT reproduce the crash; the crash mechanism is something the
  bisect doesn't exercise (e.g. a different parexec-deferred code path,
  CEF, or a UI-state interaction). Send back what we have anyway.
"""

import os
import sys
import json
import traceback
from datetime import datetime


# --- Output path -----------------------------------------------------------

_proj = project.folder
_out = os.path.join(_proj, '.embody', 'crash-bisect.json')
try:
	os.makedirs(os.path.dirname(_out), exist_ok=True)
except OSError:
	_out = os.path.join(_proj, 'crash-bisect.json')


# --- File writer that's durable across process death -----------------------

_events = []


def _flush():
	"""Write the full event log to disk and fsync, so the data survives
	an abrupt process exit on the very next instruction."""
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


# --- Pre-flight state snapshot ---------------------------------------------

def _safe(fn, default='?'):
	try:
		return fn()
	except Exception as e:
		return f'<error: {e}>'


_mcp_keys = [k for k in sys.modules if k == 'mcp' or k.startswith('mcp.')]

_meta = {
	'td_version': _safe(lambda: f'{app.version}.{app.build}'),
	'embody_version': _safe(lambda: op.Embody.par.Version.eval()),
	'os': sys.platform,
	'project_name': _safe(lambda: project.name),
	'project_folder': _proj,
	'envoy_running_flag': _safe(
		lambda: bool(op.Embody.fetch('envoy_running', False))),
	'envoyenable_par': _safe(lambda: bool(op.Embody.par.Envoyenable.eval())),
	'envoystatus_par': _safe(lambda: str(op.Embody.par.Envoystatus.eval())),
	'mcp_server_in_sys_modules': 'mcp.server' in sys.modules,
	'mcp_submodules_loaded': len(_mcp_keys),
	'pydantic_core_loaded': 'pydantic_core' in sys.modules,
	'pydantic_core_version': _safe(
		lambda: getattr(sys.modules.get('pydantic_core'), '__version__', None)),
	'venv_present': os.path.isdir(os.path.join(_proj, '.venv')),
	'output_path': _out,
}


_record('preflight', 'snapshot', json.dumps(_meta))


# --- Bisect: run each Start() step individually ----------------------------

embody_ext = op.Embody.ext.Embody
envoy_ext = op.Embody.ext.Envoy

STEPS = [
	('setupEnvironment',
		# Includes _verifyMcpImportable, i.e. the sys.modules teardown +
		# mcp.server re-import. If THIS closes TD, the v5.0.393 teardown
		# really is the killer and the fast-path patch is the fix.
		lambda: embody_ext._setupEnvironment()),
	('findAvailablePort',
		lambda: envoy_ext._findAvailablePort(9870)),
	('cleanupStaleThreads',
		# Modifies ThreadManager system COMP state (thread.clean(),
		# Tasks.remove, Runningthreads). Suspect under ThreadManager
		# v1.1.7's "keeping threads alive in case of caught exception"
		# change in TD 2025.32280.
		lambda: envoy_ext._cleanupStaleThreads()),
	('configureMCPClient',
		# Runs subprocess.run([venv_python, '-c', '...'],
		# capture_output=True, stdin=DEVNULL) on Windows. The existing
		# code documents one DuplicateHandle landmine; a sibling one on
		# stdout/stderr in TD 2025.31550's new Windows console is plausible.
		lambda: envoy_ext._configureMCPClient(9870)),
	('upgradeEnvoy',
		lambda: embody_ext._upgradeEnvoy()),
]

for _name, _fn in STEPS:
	_record(_name, 'about_to_run')
	try:
		_r = _fn()
		_record(_name, 'completed', repr(_r)[:500])
	except Exception:
		_record(_name, 'exception', traceback.format_exc())
		break


# --- Last step: full Envoy.Start() -- exercises the worker thread ---------

# If every individual step survived, the remaining variable is the worker
# thread (EnqueueTask -> _runServer -> EnvoyMCPServer -> uvicorn). Call
# Start() directly to exercise it. If the crash is in the worker thread,
# Start() will return synchronously and TD will die a few frames later --
# the deferred alive check below confirms whether that happened.
_record('full_Start', 'about_to_run',
	'individual steps survived; invoking Envoy.Start() which spawns the '
	'worker thread + uvicorn')
try:
	envoy_ext.Start()
	_record('full_Start', 'returned_synchronously')
except Exception:
	_record('full_Start', 'exception', traceback.format_exc())


# --- Async tail: confirm TD is still alive 5 seconds later ----------------

def _alive_check():
	try:
		status = str(op.Embody.par.Envoystatus.eval())
		running = bool(op.Embody.fetch('envoy_running', False))
		_record('alive_after_5s', 'survived',
			f'envoyenable={op.Embody.par.Envoyenable.eval()} '
			f'envoy_running={running} envoystatus={status!r}')
	except Exception:
		_record('alive_after_5s', 'exception', traceback.format_exc())


run('args[0]()', _alive_check, delayFrames=300)

print(f'[diagnose_envoy_toggle_crash] events written to: {_out}')
print('[diagnose_envoy_toggle_crash] If TD survives, check the file in '
	'~5 seconds for the alive_after_5s entry.')
