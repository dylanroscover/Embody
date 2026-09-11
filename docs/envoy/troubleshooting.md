# Envoy Troubleshooting

## Server Won't Start

**Symptoms:** Toggling Envoy Enable does nothing, no port number appears in the toolbar, or errors in the Textport.

1. **Check the Textport** (Alt+T) — Embody logs all startup messages there. Look for lines starting with `[Envoy]`.
2. **Dependency install in progress:** On first enable (or after a version upgrade), Envoy builds its virtual environment and installs `mcp`, `uvicorn`, and other packages. This runs in a background thread — the status reads `Installing deps... (one-time)` and the port appears only when it finishes. A fresh install can take a minute or two; wait for it rather than re-toggling. If it **failed** (e.g., no internet, Python version mismatch), the status shows `Error: Python environment not ready`. Check for the error in the Textport, then toggle Envoy off and on to rebuild. Never pip-install into `.venv` yourself — Embody owns that environment and rebuilds it (including your declared extras) on its own; see [Python Environment](../embody/python-environment.md).
   On Windows, also add `"pywin32>=311"`.
3. **Port already in use:** If another process is using port 9870 (the default), the server will fail to bind. Change the **Envoy Port** parameter on the Embody COMP to a different port (e.g., 9871).
4. **TD version too old:** Envoy requires TouchDesigner **2025.33230** or later.

## Port Climbs on Every Restart, Then Envoy Disables Itself

**Symptoms:** The Textport repeats `Envoy did not bind port NNNN within the
startup timeout`, each retry reports a higher port than the last (9870, 9871,
9872...), and after 30 minutes Envoy gives up on a port your `.mcp.json` was
never pointed at.

**Cause:** A start that ran out of its budget was abandoned but not shut down.
It bound its port moments later and held it for the life of the TouchDesigner
process, so the next attempt read that port as occupied and stepped past it --
repeatedly. A cold Python stack (first start after an install or upgrade, or a
virtual environment on a slow or virus-scanned drive) can outrun the budget on
its own, which is what starts the climb.

**Fix:** Update to **6.2.43** or later. Envoy now reclaims its own abandoned
listeners before choosing a port, shuts a timed-out start down instead of
letting it bind late, allows a cold start considerably longer to come up, and
keeps the preferred port eligible for the retry. Restart TouchDesigner once
after updating -- listeners leaked by the older build live until the process
exits.

## Envoy Stops Answering and Stays Off Across Restarts

**Symptoms:** The Textport shows `Watchdog: enabled but socket dead (status
'Running on port 9870') -- reviving`, then `Envoy did not bind port 9870 within
the startup timeout`, although the port is free. After enough retries Envoy
stops, and relaunching TouchDesigner does not bring it back until you turn
**Enable Envoy** on again.

**Cause:** The restart never got as far as the port. Every Python event loop
opens an internal loopback socket pair, and on Windows the connection inside
that step can fail without anything noticing -- measured as a port collision
on machines whose Windows dynamic (ephemeral) port range had been widened to
start at 1024 instead of the default 49152. The new server thread then waited
forever. Each hang also left a thread behind, and when Envoy finally gave up it
switched **Enable Envoy** off -- a setting Embody saves, so it stayed off in
every later session.

**Fix:** Update to **6.2.46** or later. Envoy now builds its event loop with a
deadline and retries a failed attempt, so the collision costs a second or two
instead of the server; the Textport says `Event loop creation stalled ... in
socket.socketpair()` when it happens. A startup timeout names the step the
server stopped at (`Envoy worker stalled before binding port 9870 (creating
event loop)`) and logs where it was stuck, instead of blaming the port. Giving
up leaves **Enable Envoy** on, so the next TouchDesigner launch tries again.

If an older build already switched Envoy off, turn **Enable Envoy** back on
once after updating -- the update cannot tell that apart from you turning it
off -- and restart TouchDesigner once to clear threads the old build left
stuck. To make the collision rarer, check the range with `netsh int ipv4 show
dynamicport tcp`; the Windows default starts at 49152 with 16384 ports, which
also keeps Envoy's ports 9870-9879 out of the pool.

To check a session, run these lines in the Textport:

```python
import sys, threading, traceback
names = {t.ident: t.name for t in threading.enumerate()}
print([(names[i], [f'{f.name}:{f.lineno}' for f in traceback.extract_stack(fr)[-4:]]) for i, fr in sys._current_frames().items() if names.get(i, '').startswith(('_runServer', 'envoy-loop-init'))])
```

More than one `_runServer` entry, or one ending in `accept`, is a worker stuck
by an older build (restart TouchDesigner to clear it). An `envoy-loop-init`
entry is an attempt the current build abandoned and retried; a few are
harmless.

## TouchDesigner Freezes After Deleting or Moving Embody

**Symptoms:** An `execute_python` call destroyed or reloaded the Embody COMP
(for example, to move it into another container), and TouchDesigner stopped
responding: one CPU core pegged, no new log lines, no CrashAutoSave. The AI
session that made the call waited, then reported TouchDesigner as not
responding ([issue #110](https://github.com/dylanroscover/Embody/issues/110)).

**Cause (leading hypothesis, not proven):** Envoy runs *inside* the Embody
COMP. An Envoy call executes nested in Envoy's own request handling -- inside
an open undo block, while Envoy's server waits for the answer. Destroying the
COMP that hosts that code, from inside that call, stalled TouchDesigner's own
code in the reported case. The same sequence, run a few frames later after the
call had returned, completed.

**What Envoy does now:** it refuses the synchronous form before anything
changes, with `error_code` `envoy.embody.host_destroy_refused`:

- `execute_python` code that would destroy or reload the Embody COMP, one of
  its ancestors, `/`, or Envoy's own extension DAT (`EnvoyExt`) in the same
  call. Inside `execute_python`, `me` and `parent()` *are* the Embody COMP.
- `delete_op` on any of those. `override=True` does not bypass this check.
- `exec_op_method` with `destroy`, `reload`, `changeType` or
  `progressiveUnload` on any of those.
- `set_parameter` of a reload or clone pulse (`enableexternaltoxpulse`,
  `reinitnet`, `enablecloningpulse`) on any of those.
- `import_network` with `clear_first=True` into any of those (the TDXN
  importer also refuses it on its own).

Everything else inside Embody stays allowed. The `execute_python` check reads
the code without running it, so it cannot see targets computed at run time
(`op(path_variable)`, loops over `.children`), code run through `exec`/`eval`
strings, or helper modules; it also leaves `project.load()` / `project.quit()`
and an extension re-init of Embody alone. It counts every assignment of a
variable, so reusing one name for Embody and then for another operator can be
refused -- use distinct names. When code destroys the host anyway, Envoy skips
its own post-call work, so it never removes a replacement Embody.

**Other responses from these checks:**

- `HOST-DESTROY REFUSED ... could not reach a verdict ... fails closed`: the
  check itself failed on a `delete_op`, `exec_op_method`, `set_parameter` or
  `import_network` call, so nothing ran. Retry; if it persists, check
  `get_op_errors` on the Embody COMP.
- `Remaining sub-operations skipped`: a `batch_operations` sub-operation
  destroyed or reloaded the Embody COMP, so the rest of the batch did not run.
- `host_destroyed: true` on an `execute_python` result: the code destroyed the
  Embody COMP through a form the check cannot see. Envoy skipped its post-call
  work and this server stops; it comes back from a new Embody instance if one
  loads.
- `host_reloaded: true`: the code reloaded the Embody COMP or re-initialized
  Envoy's extension. This Envoy instance stops and the new one restarts it.

**If you really mean to move or remove Embody:** relocation is not a tested
workflow, so save first (`save_project`, before the call that schedules the
move) and be ready to reopen that save.

- Put the whole sequence in ONE string and defer it:
  `run(code_string, delayFrames=30)`.
- Make `op.Embody.ext.Envoy.Stop()` the first line *inside* that string, not a
  separate call beforehand: called on its own, it cuts the AI session's
  channel before the deferred `run()` is sent.
- Inside the string, refer to Embody as `op.Embody` or by resolved absolute
  paths -- `me` and `parent()` mean something else there.
- The externalizations table (`externalizations`) must stay a sibling of the
  Embody COMP. Embody reconnects only to a sibling table; anywhere else it
  starts a new, empty one.
- Expect Envoy to come back from the new instance. Check with `get_td_status`,
  then `op.Embody.path`, then `get_externalizations`.

**Reading `get_td_status` during a freeze:**

- `envoy_unresponsive: true` (with `unresponsive_since`): TouchDesigner's
  process is alive and Envoy's port still accepts connections, but Envoy has
  not answered for 60 seconds or more. A save blocks TouchDesigner for 15-30
  seconds, so a shorter silence is not a freeze; a longer one means
  TouchDesigner looks frozen, or is stuck in one very long call.
- `main_thread_stalled: true` (with `stalled_since`): Envoy's server still
  answers, but TouchDesigner's main thread has not come back to Envoy's
  request loop for 60 seconds or more -- a blocking dialog, or a call that
  never returned.
- The session whose call froze TouchDesigner gets an answer once
  `envoy_unresponsive` has held for another 120 seconds (3 minutes of silence
  in all) instead of waiting out the 5-minute request limit. The message says
  the call was abandoned and may still complete if TouchDesigner recovers.

Call `list_dialogs` first. Never kill TouchDesigner without the user: unsaved
work is lost. A freeze that writes no CrashAutoSave can also be a masked crash
(see the td-recovery skill). Separately, deleting a live Web Client DAT that
held an open connection was reported to crash TouchDesigner (issue #110);
that is unrelated to Embody.

## Restart Loop: "Unable to configure formatter 'default'"

**Symptoms:** Envoy never comes up; the Textport repeats a traceback ending in

```
AttributeError: 'Logger' object has no attribute 'isatty'
ValueError: Unable to configure formatter 'default'
```

every ~10–25 seconds, with noticeable freezes or frame drops as the watchdog keeps restarting the server.

**Cause:** TouchDesigner replaces `sys.stdout` with a Textport catcher object, and some TD builds (confirmed on **2025.32460** on Windows) ship one **without an `isatty()` method**. uvicorn's default log formatter probes `sys.stdout.isatty()` during server construction, so startup dies before the socket ever binds — and Envoy's liveness watchdog restarts the dead server indefinitely.

**Fix:**

1. **Update Embody** to v6.0.116 or later — Envoy now passes `use_colors=False` to uvicorn, which skips the `isatty()` probe entirely.
2. **Or update TouchDesigner** to 2025.33230 or later (the documented minimum) — those builds ship a stdout catcher that implements `isatty()`.

Do **not** patch `.venv/.../uvicorn/logging.py` by hand — the edit is lost whenever the virtual environment is rebuilt (TD upgrades, dependency floor bumps, venv repair).

## Claude Code Can't Connect

**Symptoms:** Claude Code says "MCP server not found" or tool calls time out.

1. **Verify Envoy is running:** Check that the Embody toolbar shows a port number next to the Envoy toggle. If not, see "Server Won't Start" above.
2. **Check `.mcp.json`:** Look for `.mcp.json` at your AI Project Root — the git repo root by default, or the `.toe`'s folder / a custom path if you've changed the **AI Project Root** parameter. It should contain a server entry for `envoy` with the correct port. If it's missing:
    - Confirm the **AI Project Root** parameter points where you expect (a non-git project still gets `.mcp.json` written to the project folder)
    - Re-enable Envoy (toggle off, then on) to regenerate it
    - Or create it manually — see [Manual Configuration](setup.md#manual-configuration)
3. **Restart Claude Code:** After Envoy generates `.mcp.json`, you need to start a **new** Claude Code session for it to pick up the config. Run `claude` again in your project directory.
4. **Port mismatch:** Ensure the port in `.mcp.json` matches the Envoy Port parameter in TD. If you changed the port, `.mcp.json` should update automatically — but check it.
5. **Firewall or proxy:** Envoy binds to `127.0.0.1` (localhost only). If you're running Claude Code on the same machine, firewalls shouldn't be an issue. If using a remote setup, Envoy does not support remote connections.

## Git Initialization Failed

**Symptoms:** Envoy starts and MCP config is generated, but `.gitignore` and `.gitattributes` are missing.

Since v5.0.264, Envoy generates MCP and AI client config files (`.mcp.json`, `CLAUDE.md`, `.claude/`, etc.) regardless of whether a git repo exists — they are written to the project folder as a fallback. Only `.gitignore` and `.gitattributes` require a git repo.

1. **No git repo:** If you later create one, run `op.Embody.InitGit()` from the textport to generate git config and update MCP paths to point to the git root.
2. **Git init error:** If Envoy attempted to initialize git and failed, a dialog will explain the error. Common causes:
    - `git` not on your system PATH
    - Permissions issue in the project directory
    - TouchDesigner's embedded Python environment conflicting with git (Envoy strips known problematic env vars, but edge cases exist)
3. **Verify manually:** Open a terminal in your project directory and run `git rev-parse --is-inside-work-tree`. If this returns `true`, git is working and you can run `op.Embody.InitGit()` to generate the missing files.

## Curl Fallback Not Working

If Claude Code can't use MCP transport, it may try to reach Envoy via curl. Verify the server is reachable:

```bash
curl http://127.0.0.1:9870/mcp
```

You should get a response (even if it's an error about missing JSON body). If you get "connection refused," the server isn't running or is on a different port. Always use `127.0.0.1`, not `localhost` -- Envoy binds IPv4-only, and on some Windows hosts the firewall silently delays or drops connections to the unused IPv6 `localhost` address (issue #57).

## MCP Disconnects Mid-Session

**Symptoms:** Claude Code stops being able to call Envoy tools mid-conversation — tool calls fail with connection errors or timeouts, even though TD is still running and Envoy shows as enabled.

This can happen if TouchDesigner restarts, the Envoy server cycles, or the bridge process exits unexpectedly.

### Claude Code CLI

Restart the session by exiting (`Ctrl+C` or `/exit`) and running `claude` again in your project directory. The bridge will reconnect to Envoy automatically.

### Claude Code VS Code Extension

1. **Close the conversation tab** in the editor.
2. Open the **Claude Code sidebar** (click the Claude icon in the Activity Bar on the left).
3. Click the conversation to reopen it — your full message history is restored.

Reopening the conversation re-initializes the MCP connection. The message history is preserved, so you can continue where you left off.

!!! tip
    If you want a completely fresh start instead, type `/clear` in the conversation. This wipes the message history but keeps the tab open with a new MCP connection.

## Multiple Instances

### Wrong instance responding

**Symptoms:** MCP tool calls affect a different TD project than expected, or `execute_python` returns unexpected `project.name`.

1. **Check which instance is active**: Call `switch_instance` with no arguments — it lists all registered instances and marks which one the bridge is targeting.
2. **Switch to the correct one**: Call `switch_instance` with the instance name (`.toe` filename without the extension).
3. **Stale entries**: If an instance shows as "reachable" but you've already closed it, the registry entry is stale. Restarting Envoy in the running instance will clean it up.

### Port exhaustion

**Symptoms:** Envoy fails to start with a message about no available ports.

Envoy scans 10 ports (default: 9870–9879). If all are occupied, it can't start. Close unused TD instances or change the **Envoy Port** parameter to a different base (e.g., 9880).

## Broken Virtual Environment

**Symptoms:** Bridge process doesn't start at all, or starts and immediately exits. `dev/logs/envoy-bridge.log` is empty or shows a Python traceback about a missing interpreter. `.mcp.json` command points to a `.venv/` Python that doesn't work.

**Cause:** The `.venv` was created from a TouchDesigner Python installation that has since been upgraded or removed. The `home` key in `.venv/pyvenv.cfg` points to a path that no longer exists (common on Windows with versioned TD directories like `TouchDesigner.2025.32460/`).

**Fix:**

1. Delete the broken venv: `rm -rf <project_dir>/.venv`
2. Toggle Envoy off and on in TouchDesigner (or restart TD) — Envoy will recreate the venv automatically, re-installing your declared `python.extras` with it
3. Reopen your Claude Code session so the bridge reconnects with the new venv Python

!!! note
    Envoy validates the venv Python on startup. If the venv interpreter fails to execute, Envoy falls back to the system Python and logs a warning to the Textport. Check for "failed to execute" warnings after TD upgrades.

## Log Files

Embody writes detailed logs to the log folder in your project directory (default `logs/`, set by the **Log Folder** parameter). Check the most recent `<project>_YYMMDD.log` file for the full picture — the Textport ring buffer only holds 200 entries.
