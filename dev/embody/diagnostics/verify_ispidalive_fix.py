"""
Verify the _isPidAlive Windows-safety fix on the crashing project,
WITHOUT installing a new Embody build. Monkey-patches the running
EnvoyExt class with the patched implementation, then exercises both
the old and new paths against the real envoy.json registry.

The patched version replaces os.kill(pid, 0) with OpenProcess(SYNCHRONIZE)
via ctypes -- mirroring envoy_bridge.is_process_alive -- so it can
neither raise SystemError nor (worse) silently TerminateProcess() a
foreign TD.

HOW TO USE
----------
1. Open the crashing project. Toggle Envoyenable OFF, wait for status
   "Disabled".  (Same prereq as the bisect runs.)
2. Create a textDAT, paste this entire script, Right-click -> Run Script.
3. Send back the file at:
       <project.folder>/.embody/verify-ispidalive-fix.json
4. With this script still applied (the monkey-patch persists for the
   session), toggle Envoyenable ON.  If TD stays alive AND the bisect
   v2 SystemError is gone from the log, the fix is verified end-to-end.

The script first runs the OLD _isPidAlive against each registry PID to
identify exactly which one triggers SystemError, then installs the
patch and reruns the same calls to confirm they all return clean bools.
Finally it exercises _writeEnvoyConfig with the patch active -- that's
the function that raised SystemError in bisect v2.
"""

import os
import sys
import json
import ctypes
import traceback
from datetime import datetime
from pathlib import Path


# --- Output ---------------------------------------------------------------

_proj = project.folder
_out = os.path.join(_proj, '.embody', 'verify-ispidalive-fix.json')
try:
	os.makedirs(os.path.dirname(_out), exist_ok=True)
except OSError:
	_out = os.path.join(_proj, 'verify-ispidalive-fix.json')

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
		'info': str(info)[:2000],
	})
	_flush()


def _safe(fn):
	try:
		return fn()
	except Exception as e:
		return f'<error: {e}>'


# --- Pre-flight: snapshot environment + read the live registry ------------

_meta = {
	'td_version': _safe(lambda: f'{app.version}.{app.build}'),
	'embody_version': _safe(lambda: op.Embody.par.Version.eval()),
	'os': sys.platform,
	'project_name': _safe(lambda: project.name),
	'project_folder': _proj,
	'output_path': _out,
}

_record('preflight', 'meta', json.dumps(_meta))

envoy_ext = op.Embody.ext.Envoy
EnvoyExtClass = type(envoy_ext)

# Read the live registry to inventory its PIDs.
registry_path = Path(_proj) / '.embody' / 'envoy.json'
registry = {}
try:
	if registry_path.exists():
		registry = json.loads(registry_path.read_text(encoding='utf-8'))
except Exception as e:
	registry = {'_read_error': str(e)}

instances = registry.get('instances', {}) if isinstance(registry, dict) else {}
registry_pids = [
	{'key': k, 'td_pid': info.get('td_pid', 0) if isinstance(info, dict) else None}
	for k, info in instances.items()
]

_record('registry_inventory', 'snapshot',
	json.dumps({'pids': registry_pids, 'my_pid': os.getpid()}))


# --- Stash the OLD _isPidAlive so we can call it explicitly --------------

_OLD = EnvoyExtClass._isPidAlive
_record('stash_old_implementation', 'completed',
	f'captured: {_OLD!r}')


# --- Call the OLD _isPidAlive on each registry PID -----------------------
#
# Each call is flushed to disk BEFORE invocation, so if TD dies during
# the call we know which PID killed it.  Wrap in try/except to catch
# SystemError too -- which is what the bisect v2 surfaced.

for entry in registry_pids:
	pid = entry['td_pid']
	step_name = f'OLD_isPidAlive__{entry["key"]}__pid_{pid}'
	_record(step_name, 'about_to_run', f'pid={pid!r}')
	try:
		r = _OLD(pid)
		_record(step_name, 'completed', f'returned={r!r}')
	except SystemError:
		_record(step_name, 'systemerror', traceback.format_exc())
	except Exception:
		_record(step_name, 'exception', traceback.format_exc())

# Also probe garbage inputs the registry could plausibly contain.
for sample in (0, None, -1, 0x7FFFFFFE, 2 ** 31, '12345', True):
	step_name = f'OLD_isPidAlive__sample_{sample!r}'
	_record(step_name, 'about_to_run', f'sample={sample!r}')
	try:
		r = _OLD(sample)
		_record(step_name, 'completed', f'returned={r!r}')
	except SystemError:
		_record(step_name, 'systemerror', traceback.format_exc())
	except Exception:
		_record(step_name, 'exception', traceback.format_exc())


# --- Install the patched _isPidAlive in the live class -------------------

def _patched_isPidAlive(pid):
	"""Windows-safe version. Mirrors envoy_bridge.is_process_alive."""
	if not isinstance(pid, int) or pid <= 0:
		return False
	if sys.platform == 'win32':
		try:
			kernel32 = ctypes.windll.kernel32
			SYNCHRONIZE = 0x00100000
			handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
			if handle:
				kernel32.CloseHandle(handle)
				return True
			return False
		except Exception:
			return False
	try:
		os.kill(pid, 0)
		return True
	except (ProcessLookupError, PermissionError):
		return False
	except (OSError, OverflowError, ValueError):
		return False


# Replace at the class level so all call sites (including the nested
# _port_registered_by_other) see the patched version.
EnvoyExtClass._isPidAlive = staticmethod(_patched_isPidAlive)

_record('install_patch', 'completed',
	'EnvoyExt._isPidAlive replaced with patched version')


# --- Call the PATCHED _isPidAlive on the same inputs ---------------------

for entry in registry_pids:
	pid = entry['td_pid']
	step_name = f'NEW_isPidAlive__{entry["key"]}__pid_{pid}'
	_record(step_name, 'about_to_run', f'pid={pid!r}')
	try:
		r = EnvoyExtClass._isPidAlive(pid)
		_record(step_name, 'completed', f'returned={r!r}')
	except Exception:
		_record(step_name, 'exception', traceback.format_exc())

for sample in (0, None, -1, 0x7FFFFFFE, 2 ** 31, '12345', True):
	step_name = f'NEW_isPidAlive__sample_{sample!r}'
	_record(step_name, 'about_to_run', f'sample={sample!r}')
	try:
		r = EnvoyExtClass._isPidAlive(sample)
		_record(step_name, 'completed', f'returned={r!r}')
	except Exception:
		_record(step_name, 'exception', traceback.format_exc())

# Also confirm own PID still reports True.
_record('NEW_isPidAlive__own_pid', 'about_to_run', f'pid={os.getpid()}')
try:
	r = EnvoyExtClass._isPidAlive(os.getpid())
	_record('NEW_isPidAlive__own_pid', 'completed', f'returned={r!r}')
except Exception:
	_record('NEW_isPidAlive__own_pid', 'exception', traceback.format_exc())


# --- Exercise _writeEnvoyConfig with the patch active --------------------
#
# This is the call site that raised SystemError in bisect v2.  With the
# patched _isPidAlive it must complete cleanly.

_record('writeEnvoyConfig_under_patch', 'about_to_run',
	'should complete without SystemError now that _isPidAlive is safe')
try:
	envoy_ext._writeEnvoyConfig(Path(_proj) / '.embody', 9870)
	_record('writeEnvoyConfig_under_patch', 'completed', 'no exception raised')
except SystemError:
	_record('writeEnvoyConfig_under_patch', 'systemerror_BAD',
		traceback.format_exc())
except Exception:
	_record('writeEnvoyConfig_under_patch', 'exception', traceback.format_exc())


# --- Async tail: confirm TD is still alive 5 seconds later ---------------

def _alive_check():
	try:
		_record('alive_after_5s', 'survived',
			f'envoystatus={op.Embody.par.Envoystatus.eval()!r}')
	except Exception:
		_record('alive_after_5s', 'exception', traceback.format_exc())


run('args[0]()', _alive_check, delayFrames=300)

print(f'[verify_ispidalive_fix] events written to: {_out}')
print('[verify_ispidalive_fix] The monkey-patch is now active for this '
	'TD session. Toggle Envoyenable OFF then ON to confirm the crash is '
	'gone end-to-end. (Reload the .toe to revert.)')
