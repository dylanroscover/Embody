# Convoy Setup and Troubleshooting

Convoy uses a small background **host app** on each participating computer. One host app serves every Convoy-enabled TouchDesigner project for the logged-in user. It is a local helper, not a central Convoy server: if it stops, only the nodes on that computer become unavailable, while other reachable siblings continue communicating.

!!! warning "Development preview"
    The controls and runtime are still being completed. **Install or Update Host App** may not have a production-ready package in every build. Windows acceptance testing is in progress; the host app, login supervision, firewall flow, and node-to-node operation have not yet been physically validated on macOS. Use a development test deployment, not a show-critical machine, until the relevant release notes say otherwise.

## Checklist for each computer

1. Connect the computer to the same trusted LAN as the other Convoy machines.
2. Save and open each `.toe` that should appear as a node.
3. Choose an AI assistant if desired, then turn on **Enable Convoy** on each participating Embody COMP. With no assistant selected, Embody runs only the internal loopback command service Convoy needs and does not configure or launch an AI client.
4. Press **Install or Update Host App** and approve the local installation prompt.
5. Press **Start Host App** if it is not already running.
6. Allow the host app on the operating system's private/trusted network profile. Do not open Envoy's local-only port to the LAN.
7. Confirm **Host App**, **This Node Status**, and the **Convoy Status** sequence on the Embody COMP.
8. Repeat on the next computer, using the same Embody version.

There is no invitation code or Create/Join decision. Enabled nodes find the Convoy automatically and reconnect after ordinary process, address, or network interruptions.

## Host App controls

| Control | What it does |
|---|---|
| **Host App** | Reports whether the background app for this logged-in user is installed, running, stopped, or needs attention |
| **Install or Update Host App** | Installs, repairs, or updates the per-user app after local confirmation |
| **Start Host App** | Starts the installed app now |
| **Stop Host App** | Stops it and disables automatic restart; queued job records are retained |
| **Uninstall Host App** | Removes the app and login registration while retaining the local host identity and job history for a later reinstall |

Installing the host app is separate from **Enable Convoy** because it runs outside TouchDesigner and may start when the user logs in. Enabling Convoy in the wizard never performs that installation silently.

The **Host App** readout uses these user-facing states:

| Readout | What to do |
|---|---|
| `Not installed` | Use **Install or Update Host App** if your build contains a runtime package |
| `Checking...`, `Installing...`, `Installed -- starting...` | Wait for the current local action to finish |
| `Running ...` | Ready; the version and process ID may also be shown |
| `Installed -- not running (restarts within a minute)` | On Windows, the scheduled supervisor may take up to a minute; macOS LaunchAgent recovery is normally prompt. You can press **Start Host App** immediately on either platform. |
| `Installed -- stopped` | Press **Start Host App** when you want Convoy available again |
| `Installed -- no supervisor (use Install or Update)` | Run **Install or Update Host App** to repair login startup |
| `Needs repair -- managed runtime unavailable` | Install the runtime package supplied for this Embody build, then run **Install or Update Host App** |
| `Running ... -- installed by a newer Embody` or `Installed ... -- installed by a newer Embody` | Do not downgrade it from an older Embody; align versions first |
| `Managed by another supervisor` | Use the studio or Owlette process that owns startup, rather than competing with it |
| `Install failed -- see log` | Review the Embody log, then retry the repair action |

Use one dedicated logged-in user for an unattended show machine. The host app is per user, not a system service, so a different user account has a separate identity, settings, jobs, and artifact quota.

## Network and firewall requirements

Convoy is for a trusted local network. For the initial release, participating computers should be on the same local discovery domain. Guest Wi-Fi isolation, client isolation, strict VLAN boundaries, VPN routing, or blocked local discovery traffic can keep otherwise reachable machines from finding each other.

- On Windows, keep the network profile **Private** and approve only the Convoy host app on that profile.
- On macOS, grant the host app Local Network access when prompted.
- Do not expose Envoy itself; it remains local to its computer.
- Avoid manually forwarding Convoy ports to the internet.
- A changing DHCP address is expected. Convoy uses stable node identities and should reconnect automatically.

Automatic first contact assumes the LAN is trusted. Do not “test briefly” on airport, hotel, coffee-shop, conference, guest, or public Wi-Fi.

## Reading status

Exact wording can vary by release, but these are the useful categories:

| Status | Meaning and next action |
|---|---|
| **Disabled** | **Enable Convoy** is off for this node |
| **Waiting for project save** | Save the `.toe`, then wait for automatic registration |
| **Host app not installed / unavailable** | Install, repair, or start the local host app |
| **Online / Registered** | The node and host app are ready |
| **Offline** | The node is known but its TD process, host app, or network path is unavailable; automatic reconnect continues |
| **Limited** | The node is reachable but the requested capability or version contract is unavailable |
| **Incompatible** | Align Embody versions before sending work |
| **Conflict - Multiple Convoys Found** | More than one previously established Convoy is visible; do not merge them by guessing. Keep existing peers running and use the advanced local reset/rejoin recovery when it is available in your build |
| **Permission denied / approval required** | Enable the required setting locally on the target; a peer cannot grant itself TD Python or Full Shell |
| **Error** | Read **Details**, then check version, firewall, host-app health, and the troubleshooting cases below |

Several rows with the same IP address are normal when one computer has multiple `.toe` files or TouchDesigner processes. Use **Node Name**, version, and details to select the intended row.

## When nodes do not appear

Work through this list on both computers:

1. Confirm **Enable Convoy** is on and the project has been saved.
2. Confirm **Host App** says it is running for the same logged-in user that runs TouchDesigner.
3. Confirm both machines use the same Embody version.
4. Confirm both are on the same trusted LAN and are not isolated guest clients.
5. Check the private-network firewall permission on both sides. A successful connection in one direction does not prove the reverse direction is allowed.
6. Wait for automatic reconnect; do not repeatedly toggle permissions while a node is converging.
7. If several established Convoys are reported, stop and use the explicit local recovery path instead of deleting random state.

If the host app was just updated, run **Install or Update Host App** once more as the repair path, then **Start Host App**. Do not manually copy host identities or settings between computers.

## When a node keeps going offline

An offline row is retained intentionally. Common causes are a closed `.toe`, a sleeping computer, a stopped host app, Wi-Fi roaming, DHCP renewal, or a temporary cable/switch interruption. Convoy reconnects and refreshes the row when the node returns.

For unattended machines:

- Disable sleep where appropriate for the installation.
- Use a dedicated user with a deliberate login/startup policy.
- Keep TouchDesigner project launch and restart procedures separate from Remote Wake.
- Verify the full shutdown, restart, login, host-app start, and `.toe` reopen path before relying on it.

Remote Wake only leaves Perform Mode temporarily; it does not reopen a closed `.toe` or relaunch TouchDesigner.

## When remote start or restart is refused

`convoy_start_node` works from a previously confirmed local launch record. If it reports an unknown or incomplete profile, open that `.toe` normally, let the node register, and verify its status before testing remote start again. A moved or deleted `.toe`, changed TouchDesigner installation, disabled node, or repeated crash loop is refused instead of guessed around.

For `convoy_restart_node`, use the node's current runtime ID from `convoy_list_nodes` and a unique `idempotency_key`. The default `require_clean` policy refuses dirty, unsaved, or unverifiable project state. Use `save_then_restart` only when saving is intended. If several Embody nodes share one TouchDesigner process, Convoy refuses a single-node restart; restart that shared application through your normal local operating procedure.

If cancellation or the deadline arrives before the old process commits to exiting, Convoy stops without deliberately quitting that process. Under `save_then_restart`, `save_may_have_run` means the save request was accepted but its final acknowledgement was uncertain; inspect the project file before deciding whether to retry.

After the old process commits to exiting, Convoy prioritizes restoring the replacement and may return `cancel_deferred` or `deadline_deferred`. The host app keeps that restoration attempt bounded and records the commit durably. If the host app itself restarts in this window, it automatically resumes reconciliation and updates the original lifecycle job. Query that same job, or repeat with the same idempotency key, until the exact node and runtime are confirmed online. Do not create a second restart identity just because the first client wait ended.

## When Remote Wake does not happen

Check **Remote Wake** on the target node. A real TouchDesigner operation should wake the command service, while discovery, ping, status, controller listing, and job polling intentionally do not.

After the final remote operation and edit lease finish, **Remote Wake Idle Grace** waits 60 seconds by default before restoring the previous Perform state. New remote work resets the timer. If Remote Wake is off, the Host App can keep the node visible for non-waking status queries while work that needs TouchDesigner is refused.

## Controller or edit-lease conflicts

The **Controllers** column is a count of active client sessions reported with the node row, not a list of privileged users. Older preview hosts may display zero until they expose that aggregate. Read-only work can overlap, but mutations acquire an edit lease for the target scope. A lease conflict means another controller currently owns the required mutation scope.

- Do not bypass the refusal by switching to a less specific target name.
- Check the detailed controller list if `convoy_list_controllers` is present in your build.
- Wait for the active work to finish or the abandoned session to expire, then inspect the target before retrying.
- Use an exclusive batch for a change that must not be interleaved with another controller.

Detailed controller listing is non-waking. It is part of the intended public tool surface but is not present in every preview build.

## Jobs, timeouts, and cancellation

A Convoy tool call can return before the remote work finishes. Keep its delivery/job ID and use `convoy_get_job` to reconcile it after a timeout or reconnect. A timeout means “the client stopped waiting,” not “the operation definitely did not run.”

Never submit a new mutation merely because the first response was lost. First query the existing job or repeat with the same returned retry identity. If the final state remains uncertain, inspect the target before making another change.

Cancellation is definitive for queued work but may be best effort once TouchDesigner or a computer action has started. The current bridge exposes `convoy_cancel_job`; older installed preview builds may not. A committed lifecycle restart is a special case: cancellation is reported as deferred while Convoy restores the replacement, as described above. Cancellation is not an operational safety system, so inspect the target before retrying any uncertain mutation.

## Permission refusals

| Refusal | Resolution |
|---|---|
| TD Python not allowed | Turn on **Allow Execute TD Python** locally on that target node, only for as long as needed |
| Full Shell not allowed | Turn on **Allow Full Shell** locally on that computer, only for an explicitly reviewed workflow |
| Git/GitHub operation unavailable | Use a supported structured action, verify the registered repository and local CLI authentication, and align versions |
| Version/capability mismatch | Update nodes to the same Embody release |
| Node in Perform Mode with wake disabled | Turn on **Remote Wake** locally or leave Perform Mode manually |

TD Python and Full Shell are separate dangerous capabilities. TD Python can use operating-system process APIs, so turning Full Shell off does not make arbitrary Python safe.

## Optional Owlette API access

Owlette is not required for Convoy LAN communication. In the current preview, its read-only inventory and status bridge uses an `OWLETTE_API_KEY` available to the Convoy host app process. After configuring the host environment, restart the host app and call `convoy_owlette` with `action=capabilities` to verify the connection. `OWLETTE_SITE_ID` may be set as an optional default.

Command submission is off independently. To opt in on that computer, set `EMBODY_CONVOY_ALLOW_OWLETTE_COMMANDS=1` in the same host environment and restart the app. Each submitted command still needs an idempotency key. Remove or set the opt-in false and restart to return the bridge to read-only use. Convoy does not expose Owlette's generic MCP command as a relay.

The preview does not yet provide a cross-platform credential-configuration control in the Embody UI. Ensure a supervised host app actually receives the intended environment without placing API keys in a `.toe`, repository, command prompt transcript, or AI request. Physical Apple Silicon macOS validation remains pending.

## Artifact quota problems

The default managed **Artifact Quota** is 1024 MB per logged-in user/computer across all local nodes. It covers screenshots, transferred files, large results, command output, partial transfers, and local materialized copies.

Relayed PNG/JPEG screenshots are normally materialized automatically and returned to the AI client as image content. Inline screenshots do not retain or report a bridge temporary path. For a large JSON/text result or another file, call `convoy_get_artifact` with the artifact reference and exact target from the original result. A successful download is verified and returns a temporary path on the computer running the local Envoy bridge; it is never a remote-host path.

To keep an artifact, call `convoy_save_artifact` with that same reference and exact target. It saves only into the current client's registered project at `.embody/convoy/artifacts/`. A custom name must be one plain filename. Existing files are protected by default; use `overwrite=true` only when you intend to replace one.

Do not try to open a Windows or macOS path reported by the target as though it existed on the client computer. If a result contains only a remote path and no artifact reference, the operation has not produced a transferable Convoy file result.

When space is needed, Convoy removes least-recently-used unpinned entries. Active transfers, running or unacknowledged job data, and explicitly saved project copies are protected. If all candidates are protected, the incoming artifact is refused.

You can:

- Increase **Artifact Quota (MB)** locally if the machine has room.
- Wait for active jobs/transfers to finish so temporary pins can be released.
- Use `convoy_save_artifact` to save anything you need under `.embody/convoy/artifacts/` before cache cleanup.
- Set the quota to `0` to disable storage-backed artifact operations. Zero is not unlimited.

Managed cache locations:

| Platform | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Embody\Convoy\cache\artifacts` |
| macOS | `~/Library/Caches/Embody/Convoy/artifacts` |

Project-saved artifacts are not counted against this quota and are never removed by cache cleanup.

## Repair, stop, and uninstall

**Install or Update Host App** is also the repair action. It should preserve the computer's Convoy identity and durable records while refreshing the installed runtime.

**Stop Host App** intentionally prevents its login supervisor from immediately starting it again. Existing queued job records remain. Press **Start Host App** to resume.

**Uninstall Host App** removes the background app and login registration but retains the local host identity and job history so a later reinstall rejoins as the same host. Review the confirmation shown by your build before proceeding. Explicitly saved project artifacts are project files and are not part of host-app cache cleanup.

## Platform validation status

Convoy is designed for Windows x64 and Apple Silicon macOS in every direction: Windows/Windows, Windows/macOS, macOS/Windows, and macOS/macOS. Cross-platform behavior is a release requirement, but design intent and automated tests are not the same as hardware acceptance.

At the time of this preview:

- Production runtime packaging is not complete in every build.
- Windows-to-Windows physical acceptance is in progress.
- Physical Apple Silicon macOS validation is pending.
- Intel macOS is not the first supported Convoy host target.

Consult the release notes for the combinations certified by the build you plan to deploy.

## Related guides

- [Convoy overview and daily use](index.md)
- [Convoy Parameter Reference](../embody/parameters.md#convoy)
- [Setup Wizard](../embody/setup-wizard.md)
- [Envoy Troubleshooting](../envoy/troubleshooting.md)
