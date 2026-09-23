# Convoy Setup and Troubleshooting

Convoy uses a small background **host app** on each participating computer. One host app serves every Convoy-enabled TouchDesigner project for the logged-in user. It is a local helper, not a central Convoy server: if it stops, only the nodes on that computer become unavailable, while other reachable siblings continue communicating.

!!! warning "Development preview"
    The controls and runtime are still being completed. Windows acceptance testing is in progress. On macOS, host-app install and the daemon runtime have been validated on Apple Silicon hardware; login supervision, the firewall flow, and node-to-node LAN operation have not yet completed physical acceptance there. Use a development test deployment, not a show-critical machine, until the relevant release notes say otherwise.

## Checklist for each computer

1. Connect the computer to the same trusted LAN as the other Convoy machines.
2. Save and open each `.toe` that should appear as a node.
3. Choose an AI assistant if desired, then turn on **Enable Convoy** on each participating Embody COMP. With no assistant selected, Embody runs only the internal loopback command service Convoy needs and does not configure or launch an AI client. Convoy runs on Envoy either way: the host app runs in the Python environment Envoy builds, and remote work reaches TouchDesigner through that command service. Enabling Convoy turns **Enable Envoy** on if it is off. A project that arrives with Convoy already enabled but Envoy off (a clone whose local settings kept Envoy off) shows `Needs Envoy` in **Status** until you turn Envoy on.
4. Approve the one-time confirmation. Enabling Convoy installs and starts the background host app automatically -- the confirmation (or the Setup Wizard's Convoy step) is the consent for the app and its login persistence.
5. Allow the host app on the operating system's private/trusted network profile. Do not open Envoy's local-only port to the LAN.
6. Confirm **Status** and the **Convoy Nodes** sequence on the Embody COMP.
7. Repeat on the next computer, using the same Embody version.

There is no invitation code or everyday Create/Join decision -- an explicit Join appears only in the [Resolve Realm Conflict recovery dialog](#recovering-from-a-realm-conflict). Enabled nodes find the Convoy automatically and reconnect after ordinary process, address, or network interruptions.

## Host App controls

| Control | What it does |
|---|---|
| **Status** | One combined readout: a blocking or in-flight host-app state (`Not installed`, `Installing...`, `Needs repair ...`) wins; otherwise the node's own registration state shows (`Connected`, `Waiting for project save`, ...) |
| **Repair Convoy App** | Reinstalls or upgrades the per-user app. Enabling Convoy installs it automatically and updates keep themselves current (see below), so this pulse is the manual repair path |
| **Start Convoy App** | Starts the installed app now |
| **Stop Convoy App** | Stops it and disables automatic restart; queued job records are retained |
| **Uninstall Convoy App** | Removes the app and login registration while retaining the local host identity, job history, the host log, and the dedicated Convoy runtime venv, so a later reinstall rejoins as the same host and reuses the runtime. The confirmation names each retained path before anything is removed |
| **Forget Offline Nodes...** | Removes this machine's offline node rows after a confirmation that names them (the first eight, then a count); the node list refreshes within a few seconds of confirming. A forgotten node rejoins as a new identity (TD Python approval resets); a node with a delivery that has not FINISHED is kept and named afterwards (a finished result never holds a row -- results are fetched by delivery id and outlive it), and a node back online before you confirm is skipped. **Rows on other machines are forgotten there, for you.** Each row is forgotten by the host that owns it, so the dialog lists every offline row in the fleet with its owner and relays each one over that owner's live session; only a row whose owner is not connected right now is left, and the dialog names that machine. This is the manual path for offline rows whose project files still exist -- deleted projects and long-unseen rows clear automatically |

Enabling Convoy installs and starts the host app automatically, because Convoy cannot reach the LAN without it. The consent is carried by the one-time Enable Convoy confirmation (or the wizard's Convoy step), which discloses that the app runs at login, whether or not TouchDesigner is open. The pulses above remain for repair, deliberate stop/start, and uninstall.

**The app keeps itself current.** The daemon reports the version of the code it is actually running, and when a project registers with a daemon running *older* code than its own Embody, that Embody updates the app in place automatically -- once per TouchDesigner session, a few seconds after registration settles, logged when it happens. Strictly older only: an equal or newer app is never touched, and a newer app is never downgraded by an older Embody.

**And it verifies the daemon it restarted, without crying wolf.** After every install or repair the restarted daemon is asked what version it now runs -- but the answer is not taken from the first read. A daemon relaunched by its supervisor during the install window can genuinely still be serving the outgoing payload for a second or two (the launcher resolves its payload directory from the install record, which is written last), so Embody waits for the reported version to converge. If it does not, Embody performs the repair itself: one graceful stop and start, so the daemon re-reads the record and picks up the new payload. Only a mismatch that survives *both* is reported -- and it is reported on **Status**, as `Needs repair ...`, not only in the log. A daemon that is busy running work is never restarted underneath it; the mismatch is reported instead.

The host-app portion of **Status** uses these user-facing states:

| Readout | What to do |
|---|---|
| `Not installed` | Enable Convoy (or press **Repair Convoy App**). The app is plain Python and runs under a dedicated per-user Convoy venv, built once on the first install; that build may download the pinned cryptography package. If it cannot be built (no uv, no Python 3.11+ outside TouchDesigner, or no network), the installer falls back to Embody's own managed Python environment and says so in the log -- see below |
| `Checking...`, `Installing...`, `Repairing runtime...`, `Installed -- starting...` | Wait for the current local action to finish. (`Repairing runtime...` is the runtime-only repair: it re-resolves the interpreter and re-registers login startup, and deliberately writes **no** payload, so the installed version does not change.) If an action ever reports "another Convoy host action is still running" long after the work should have finished, simply retry: a stuck request self-heals -- a finished result is delivered on the next attempt, and a truly dead action releases its slot within about 15 minutes. No TouchDesigner restart is needed |
| `Running ...` | Ready; the version and process ID may also be shown |
| `Installed -- not running (restarts within a minute)` | On Windows, the scheduled supervisor may take up to a minute; macOS LaunchAgent recovery is normally prompt. You can press **Start Convoy App** immediately on either platform. |
| `Installed -- stopped` | Press **Start Convoy App** when you want Convoy available again |
| `Installed -- no supervisor (use Repair Convoy App)` | Run **Repair Convoy App** to repair login startup |
| `Needs repair -- still running X, not Y (use Repair Convoy App)` | The new payload is on disk, but the daemon serving the machine is still running the older code -- and it stayed that way through the settle wait *and* one automatic restart. Press **Repair Convoy App**; if it persists, check the log for why the supervisor cannot replace the process |
| `Needs repair -- Python not found (reinstall)` | The Python the app was installed against is gone. Run **Repair Convoy App** to re-resolve the runtime; it is also the repair path if Embody's Python environment was rebuilt. This works even when the app was installed by a *newer* Embody than the project you are in: that case re-resolves the runtime and re-registers login startup **without** writing a payload or changing the installed version, so the newer app is what starts back up. The one case it cannot fix is an app managed by another supervisor -- repair that through the supervisor that owns it |
| `Running ... -- installed by a newer Embody` or `Installed ... -- installed by a newer Embody` | Do not downgrade it from an older Embody; align versions first. (If its recorded Python is *missing*, the readout is `Needs repair ...` instead and **Repair Convoy App** will fix it in place -- see the row above) |
| `Managed by another supervisor` | Use the studio or Owlette process that owns startup, rather than competing with it |
| `Install failed -- see log` | Review the Embody log, then retry **Repair Convoy App** |
| `Consent required -- enable Convoy again` | The first-enable confirmation was declined or never answered; toggle **Enable Convoy** on again to see it |

If the log says `no interpreter on this machine could load cryptography and TLS 1.3`, the install probed every Python it could find and names each one with why it failed. Three causes are distinguished:

- `runtime_missing_cryptography` -- that Python simply has no cryptography package (normal for a bare system `python3`; when Embody's own venv reports this, the installer attempts the same one-shot repair described below).
- `runtime_crypto_broken` -- cryptography is installed but cannot load, typically a CPU-architecture mismatch in Embody's `.venv` (for example after switching between the Intel and Apple Silicon TouchDesigner builds). The installer automatically attempts one repair (reinstalling the pinned cryptography into the venv); if that still fails, toggle **Enable Envoy** off and on -- Embody detects an architecture change and rebuilds the Python environment -- then enable Convoy again.
- `runtime_crypto_signature_blocked` -- macOS refused to load cryptography into TouchDesigner's bundled Python: that binary is code-signed with library validation and, run standalone, may not load third-party native modules (`... different Team IDs` in the log). Reinstalling or rebuilding the venv cannot fix this. Convoy handles it by building a dedicated daemon venv (at the Convoy data directory's `runtime-venv`) from a Python outside TouchDesigner -- Homebrew's `python3`, probed at `/opt/homebrew/bin` and `/usr/local/bin` by absolute path, or Apple's Command Line Tools `python3` once it reaches 3.11 (today's 3.9.6 is version-gated out). If no usable Python exists, install one (`brew install python`) and enable Convoy again.

**Windows builds the same venv, for a different reason.** Nothing on Windows refuses to load cryptography, so the daemon venv is not about code signing there -- it is about *durability*. One machine has one Convoy host app, and with no signed managed runtime installed it would otherwise run under the `.venv` of whichever project happened to install it. That project path is recorded in the machine-wide `installed.json` and in the Scheduled Task's `<Command>`, so moving, renaming, rebuilding or deleting that one project stops the machine's daemon at the next logon -- silently, because the supervisor keeps launching a Python that is no longer there. Convoy therefore prefers a per-user `runtime-venv` on Windows too, built from the first Python 3.11+ it finds outside TouchDesigner: a python.org install under `Program Files` or the per-user `Programs\Python` folder, then a uv-managed CPython, and finally TouchDesigner's own bundled Python as a last resort. It never uses the Microsoft Store `python3` alias.

If that build cannot happen -- no uv, no Python 3.11+, or no network for the cryptography wheel -- the install still succeeds under the project venv rather than refusing, and the log says exactly what it fell back to and what that costs. This matters on a show LAN or a locked-down studio, which is the same reason the daemon itself is vendored rather than downloaded.

The daemon venv is built once per user per machine and reused by every later install, update, and repair: a healthy existing venv passes its probe in seconds, works offline, and is retained by uninstall. Only the first build (or a rebuild after deleting it) downloads packages.

Use one dedicated logged-in user for an unattended show machine. The host app is per user, not a system service, so a different user account has a separate identity, settings, jobs, and artifact quota.

### Supervisor-launched TouchDesigner (Owlette, service wrappers)

A TouchDesigner started by a process supervisor rather than a double-click -- [Owlette](https://owlette.app), NSSM, a studio's own relauncher -- is a first-class session: enabling Convoy installs and starts the host app from it like any other, with no console visit. (Releases before 6.0.257 failed here with `OSError: [WinError 50]` on every interpreter candidate and blamed the session; the actual cause was Embody's own spawn call inheriting the supervisor's stdin handle, fixed in 6.0.257.)

If an install ever reports that the operating system refused to start a child process, the failure message now carries the session facts needed to diagnose a headless machine remotely -- session id, whether a console is attached, Job object membership and limit flags, the active-process cap, and whether breakaway is permitted. Include that line when reporting the problem.

A host app that is already installed and running is attached to, not reinstalled: TouchDesigner discovers it through the per-user data directory and a local health probe, spawns nothing, and skips install and start entirely. When login startup is owned by an external supervisor, **Status** shows `Managed by another supervisor` (see the table above) and repair belongs to that supervisor.

## Network and firewall requirements

Convoy is for a trusted local network. For the initial release, participating computers should be on the same local discovery domain. Guest Wi-Fi isolation, client isolation, strict VLAN boundaries, VPN routing, or blocked local discovery traffic can keep otherwise reachable machines from finding each other.

For a locked-down studio firewall that needs explicit rules, the LAN transport uses two fixed defaults:

| Traffic | Default | Notes |
|---|---|---|
| Peer-to-peer | inbound **TCP 47600** | The mutually authenticated TLS transport between host apps |
| Discovery | **UDP 47601**, multicast group **239.255.67.86** | TTL 1, so it is never routed off the local segment |

Allow both on the private/trusted profile. The host app's own API stays on `127.0.0.1` and needs no firewall rule at all.

- On Windows, keep the network profile **Private** and approve only the Convoy host app on that profile.
- On macOS, grant the host app Local Network access when prompted.
- Do not expose Envoy itself; it remains local to its computer.
- Avoid manually forwarding Convoy ports to the internet.
- A changing DHCP address is expected. Convoy uses stable node identities and should reconnect automatically.

Automatic first contact assumes the LAN is trusted. Do not "test briefly" on airport, hotel, coffee-shop, conference, guest, or public Wi-Fi.

## Reading status

Exact wording can vary by release, but these are the useful categories:

| Status | Meaning and next action |
|---|---|
| **Disabled** | **Enable Convoy** is off for this node |
| **Waiting for project save** | Save the `.toe`, then wait for automatic registration |
| **Connected -- N admitted peer(s) reject this host's certificate ...** | This host's Convoy identity changed (its data directory was lost or reinstalled) and those peers still pin the old one, so every connection they open to this host fails the TLS handshake. On EACH of those machines press **Re-pin Changed Peers...** -- see [Identity changes and re-pinning](#identity-changes-and-re-pinning) |
| **Connected -- N admitted peer(s) still dial a previous identity of this host ...** | Those peers already trust this host's current identity (a live session proves it) but also keep an older record of it at the same address, from before its `host.json` was lost. Each peer parks that ghost as dormant on its next failed dial (`peer_dormant` in its audit); a peer on an older host app needs `POST /peers/forget` for the old host_id -- see [Identity changes and re-pinning](#identity-changes-and-re-pinning) |
| **Offline -- TD running but not cooking** (a node row) | The node's TouchDesigner process is alive but has sent no heartbeat for a minute: its frame loop stopped. Usually the window is minimized with TouchDesigner's "Stop Playing when Minimized" preference on, or the timeline is paused, or a modal dialog is up. Restore the window or dismiss the dialog; the node is back within one heartbeat. `offline_reason` reads `stalled` |
| **Connected -- N dormant peer(s): their address now serves another pinned identity ...** | This host stopped dialing N pinned peers because the address on their record answers with a different pinned identity (a re-minted host, or an address reassigned to another Convoy machine). Nothing about them changed but the dialing: the pin, state and queued work stay. They wake on their own the moment the identity connects or announces again; if a machine is gone for good, `POST /peers/forget {host_id}` removes its record |
| **Connected -- new host identity: N pinned peer(s) refuse this host ...** | The same fact seen from the host that re-minted: it found pinned peers on record when it minted a new identity. Re-pin it on each of those machines; the line clears after 14 days |
| **Connected -- N pinned peer(s) changed identity: use Re-pin Changed Peers...** | A peer of this machine announces a new identity. Confirm the change with its operator, then press **Re-pin Changed Peers...** here |
| **Connected -- realm conflict (N realms): use Resolve Realm Conflict...** | An admitted peer of a realm this machine once shared now advertises that realm while this machine sits in another: the split is latched. Follow [Recovering from a realm conflict](#recovering-from-a-realm-conflict); this node keeps working in the preserved realm meanwhile |
| **Connected -- LAN listener on <address> (<adapter>, tunnel adapter): set bind in lan.json to move it** | `lan.json` names a VPN, virtual or Public-network adapter explicitly. Peers on the real LAN cannot reach it; change or remove `bind` |
| **Connected -- LAN listener refused: lan tunnel only / lan public network** | Auto-bind found only a tunnel adapter, or the routed adapter sits on a Windows network classed Public, and refused to expose the listener there. Connect the LAN, set the network profile to Private, or name the interface in `lan.json` deliberately -- see [The LAN listener and VPNs](#the-lan-listener-and-vpns) |
| **Needs Envoy** | **Enable Envoy** is off. Convoy runs on Envoy: the host app runs in the Python environment Envoy builds, and remote work reaches TouchDesigner through Envoy's local command service. Turn **Enable Envoy** on; the host app then installs or updates itself and the node registers. Until then the node is registered but offline to every controller, and its row on other machines reads `Offline -- no Envoy relay port` |
| **Host app not installed / unavailable** | Install, repair, or start the local host app |
| **Online / Registered** | The node and host app are ready |
| **Offline** | The node is known but its TD process, host app, or network path is unavailable; automatic reconnect continues |
| **Limited** | The node is reachable but the requested capability or version contract is unavailable |
| **Incompatible** | Align Embody versions before sending work |
| **Refused: local_realm_conflict** (realm conflict / multiple Convoys) | This is the exact string that appears in the affected node's combined **Status** readout. Another established Convoy has been reported by evidence this machine trusts -- an admitted peer, or this machine's own saved node records -- so registration is refused rather than merged. A stranger on the LAN advertising a foreign Convoy can no longer latch this state; it is recorded as a powerless, audited advisory, which **Resolve Realm Conflict...** also lists and can denylist or join. Do not merge realms by guessing. Keep the existing peers running and use the Convoy page's **Resolve Realm Conflict...** action, which names the conflicting realms and their live senders (with hostnames where reverse DNS answers) and offers both directions: **Keep This Realm** denylists the live senders and resets, while `Join <realm id>` abandons this machine's realm and adopts the other one -- the right choice on a machine that crowned itself in isolation and now meets the mesh it should be on. Full sequence: [Recovering from a realm conflict](#recovering-from-a-realm-conflict) |
| **Permission denied / approval required** | Enable the required setting locally on the target; a peer cannot grant itself TD Python or Full Shell |
| **Error** | Read **Details**, then check version, firewall, host-app health, and the troubleshooting cases below |

Several rows with the same IP address are normal when one computer has multiple `.toe` files or TouchDesigner processes. Use **Node Name**, version, and details to select the intended row.

## When nodes do not appear

Work through this list on both computers:

1. Confirm **Enable Convoy** is on and the project has been saved. Disabling the last enabled node on a computer withdraws that whole computer from the LAN: the host app keeps running for local use but closes its LAN listener and stops announcing, so none of its rows can be seen, pinged, or remotely started until a node there is enabled again.
2. Confirm **Enable Envoy** is on. **Status** reads `Needs Envoy` while it is off, and the node's row on other machines reads `Offline -- no Envoy relay port`.
3. Confirm **Host App** says it is running for the same logged-in user that runs TouchDesigner.
4. Confirm both machines use the same Embody version.
5. Confirm both are on the same trusted LAN and are not isolated guest clients.
6. Check the private-network firewall permission on both sides. A successful connection in one direction does not prove the reverse direction is allowed.
7. Wait for automatic reconnect; do not repeatedly toggle permissions while a node is converging.
8. If several established Convoys are reported, stop and follow [Recovering from a realm conflict](#recovering-from-a-realm-conflict) instead of deleting random state.

If the host app was just updated, run **Repair Convoy App** once more as the repair path, then **Start Convoy App**. Repair works while the host app is running: it asks the old daemon to exit gracefully, waits, and replaces it -- no manual stop needed. Do not manually copy host identities or settings between computers.

## Recovering from a realm conflict

A machine that established its own Convoy in isolation -- powered up alone, or on a disconnected switch -- and later meets the mesh it should have joined ends up refusing registration with `Refused: local_realm_conflict`. Convoy will not merge two established realms by guessing, so the way out is an explicit, operator-confirmed sequence run **on the machine that is on the wrong realm**. Keep the other machines running and announcing throughout, and work through all four steps in order.

Two things keep this rare. A host whose realm record is missing (a lost `realm.json`) consults the peers it has admitted before founding anything: when they are recorded in one established realm it rejoins that realm, and when they disagree it latches a conflict instead of founding a realm of one. And an admitted peer, pin intact, that advertises a realm this host recorded it in is treated as evidence of a split: the conflict latches and the node's **Status** reads `Connected -- realm conflict (N realms): use Resolve Realm Conflict...` (a project already bound to the preserved realm keeps working). Only a stranger -- unknown, pin mismatch, or admitted to some other realm -- is still logged as an advisory (`realm_foreign_advisory`, now naming the sender's standing) and ignored.

1. **Pulse Resolve Realm Conflict... on the Convoy page and choose Keep This Realm.** This silences the live senders of the foreign realm (they are added to the denylist) and then resets this machine's conflict record. Do this first even though you intend to join the other realm: while the conflict is latched and senders keep re-latching it, nothing else settles.
2. **Turn Enable Convoy back on.** The refusal left this node disabled, and a refused registration never re-enables a node by itself -- the refusal is returned before the row is re-enabled. Nothing in the following steps can work while the node is down.
3. **Pulse Resolve Realm Conflict... again, while the other machines are on and announcing.** A `Join <realm id>` button is offered only for a standard realm id with a *live* announcer. The realm ids latched in the conflict record are display-only, and the host app refuses to adopt an id nobody is announcing -- it answers "run the join again while the other machine is on and announcing". If no Join button appears, the mesh is not being heard right now; fix that before continuing.
4. **Choose `Join <realm id>` for the mesh this machine belongs on.** This machine abandons its own realm and adopts that one. Every project on the machine moves: the current project rebinds now, and the others offer their rejoin on their next Convoy enable.

One step is easy to miss: **the Keep This Realm from step 1 left a denylist entry for the machine you just joined.** Remove it (see below) or this computer stays deaf to its new mesh. Embody warns about this in the join dialog and again in the log, naming the senders that need clearing.

A related case needs a different, much shorter fix: a *project* on the wrong realm rather than a machine. A repo cloned from another studio's LAN carries its committed binding with it and is refused here as well, but the machine itself is healthy. Toggle **Enable Convoy** on and take the **Rejoin Local Convoy** offer instead of the sequence above -- see [How membership works](index.md#how-membership-works).

### The denylist (denylist.json)

**Keep This Realm** and **Denylist Senders** write to `denylist.json` in the Convoy data directory:

| Platform | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\EmbodyConvoy\denylist.json` |
| macOS | `~/Library/Application Support/EmbodyConvoy/denylist.json` |

It is a small, deliberately hand-editable file -- Convoy's own dialogs and log lines tell you to edit it -- so it is worth knowing its rules:

- An entry blocks by **host id** or by **certificate fingerprint**, and neither subsumes the other: a fingerprint entry survives the peer changing its host id, a host-id entry survives it rotating its key.
- **A missing file blocks nobody.** That is the normal state of a machine that has never blocked anything.
- **An unreadable or malformed file fails closed and refuses every peer** until it is fixed by hand. If a machine suddenly sees nothing after a manual edit, check that file's syntax before anything else.
- Convoy validates entries as it writes them, and refuses to append to a file that is already failing closed.
- After joining a realm you previously kept against, remove that realm's machines from the file. A stale entry silences exactly the mesh you just joined.

## Identity changes and re-pinning

A host app's Convoy identity is the key pair in `identity.key` / `identity.cert.pem` beside `host.json` in its data directory (`%LOCALAPPDATA%\EmbodyConvoy` on Windows, `~/Library/Application Support/EmbodyConvoy` on macOS). Peers pin that identity on first contact and never update the pin on their own -- a changed key is exactly what an impersonator would present. So when the identity files are lost (nothing in Embody deletes them; a re-mint has been seen in the field after the directory was emptied by hand or by a cleanup tool) the host mints a new identity at the next start, and from then on:

- every peer that pinned the old key refuses this host's certificate on every connection it opens to it (`peer_handshake_refused` in this host's `audit.jsonl`, `TLSV1_ALERT_UNKNOWN_CA`), and this host's own connections to them fail with `pin_mismatch`;
- this host audits `identity_reminted` with the count of pinned peers, and its nodes read `Connected -- new host identity: N pinned peer(s) refuse this host until they re-pin it` for 14 days;
- once the handshakes start failing, its nodes also read `Connected -- N admitted peer(s) reject this host's certificate (M refusals/10 min)`, and `get_convoy_status` reports `inbound_refusals` with the sources mapped to admitted peers;
- each peer hears the new identity in this host's discovery beacon, records it (`peer_identity_changed`), and reads `Connected -- N pinned peer(s) changed identity: use Re-pin Changed Peers...`.

To repair it, on **each machine that pinned the old identity**, confirm with the re-minted host's operator that the change is real, then press **Re-pin Changed Peers...** on the Convoy page. The confirmation names every changed peer (hostname where reverse DNS answers, address, the pinned and the offered fingerprint); **Re-pin All** trusts the new identities, revokes any work the old keys had queued, and reconnects. The host app exposes the same as `GET /peers/mismatched` and `POST /peers/repin {host_id}` on its loopback API. Never copy identity files between machines to "fix" this, and never re-pin a peer whose change nobody can explain.

When the whole data directory was lost (not just the key), the host also mints a **new host_id**, so to its peers it is a brand-new host rather than a changed one: discovery admits the new identity beside the old record, every peer's work reaches the new host normally, and the old record becomes a ghost that each peer keeps dialing at the same address. Each peer parks that ghost as **dormant** by itself the first time a dial to it fails the pin while another pinned record holds that address and the ghost has not connected since that record's pin was first seen (`peer_dormant` in the peer's `audit.jsonl`; the reborn host's Status reads `N admitted peer(s) still dial a previous identity of this host` until they do). Dormant is deliberately not forgotten: the pin, the state and any queued work stay, so an impersonator of the old identity still trips `pin_mismatch` and nobody can claim the old host_id by trust-on-first-use; only the dialing stops, and any contact from the identity itself -- an inbound connection, a discovery announcement, a re-admit -- wakes it (`peer_woken`). A peer running a host app older than 6.2.62 keeps dialing, and a machine that is gone for good keeps its dormant record until an operator removes it: both are `POST /peers/forget {host_id}` there.

## The LAN listener and VPNs

With no `lan.json`, the host app binds its LAN listener automatically as soon as one local node has **Enable Convoy** on. The address comes from the interface the OS would route outbound traffic through -- which is the LAN on a plain machine and a VPN's tunnel adapter the moment a VPN pushes routes (TEC-A4D sat on an OpenVPN TAP adapter, unreachable from the LAN, for eight days in September 2026 while every panel read Connected). Since 6.2.61 the host app also asks the OS what owns that address (PowerShell's NetAdapter cmdlets on Windows, `ifconfig`/`route` on macOS, `ip` and sysfs on Linux, refreshed every ten minutes):

- a physical adapter on a Private or domain network keeps the probe's answer;
- a tunnel or virtual adapter (TAP-Windows, Wintun, WireGuard, OpenVPN, Tailscale, Hyper-V, VMware, ...) or a Windows network classed Public is skipped for a physical adapter that is up, holds a usable IPv4 and is not Public -- the one holding the default route first;
- when nothing else qualifies, the listener is **refused by name** (`lan_tunnel_only`, `lan_public_network`) and loopback service continues. Connect the LAN, set the Windows network profile to Private, or name the interface in `lan.json` to bind it deliberately.

An explicit `bind` in `lan.json` is always honoured as written and merely described: the node's **Status** and `get_convoy_status` (`host_status.lan`) say which adapter it sits on and warn when it is a tunnel or a Public network. `host.log` names the adapter and network category on every bind (`convoy LAN: peer listener on 192.168.88.10:47600 via Intel(R) I211 Gigabit Network Connection [Private] ...`), every line of it now carries a timestamp, and the audit event `lan_endpoint_changed` records the adapter on every move.

## Isolating Convoy for tests and diagnostics

Set `EMBODY_CONVOY_DATA_DIR` to a throwaway directory and every Convoy data-dir resolver honours it: TouchDesigner's client, the Envoy bridge, the installer and a host app started with no `--data-dir`. Embody then sees no host app there and **refuses to install a login host app for it** (the login supervisor is per user; a second one would replace the real one). The release smoke's `--isolate-convoy` uses this, and the pytest tier sets it automatically. A daemon can still be run there by hand: `python convoy_hostapp.py --data-dir <dir>`.

## When a node keeps going offline

An offline row is retained intentionally. Common causes are a closed `.toe`, a sleeping computer, a stopped host app, Wi-Fi roaming, DHCP renewal, or a temporary cable/switch interruption. Convoy reconnects and refreshes the row when the node returns.

Retention has two automatic limits: a node whose `.toe` has been **deleted from disk** is forgotten by the host app's retention sweep after about half an hour of silence, and any node **unseen for 7 days** is forgotten regardless. Both respect the same safety rules as manual forgetting -- a node with a delivery that has not finished is never removed, and an unplugged or unmounted drive is never treated as a deletion (recognized by drive letters and the standard mount locations `/Volumes`, `/mnt`, `/media`; a volume mounted at a custom path is not distinguishable from a deleted folder, so prefer keeping such projects on standard mounts). For immediate cleanup of a specific stale row, an AI session can call `convoy_forget_node` with the node's id (from `convoy_list_nodes`), adding its `host_id` for a row another machine owns; it refuses while the node still has an unfinished delivery, and names the blocking delivery ids so they can be cancelled.

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

Per-node controller counts are not a column in the **Convoy Nodes** sequence; use `convoy_list_controllers` for the live client sessions attached to a node. Read-only work can overlap, but mutations acquire an edit lease for the target scope. A lease conflict means another controller currently owns the required mutation scope.

- Do not bypass the refusal by switching to a less specific target name.
- Check the detailed controller list if `convoy_list_controllers` is present in your build.
- Wait for the active work to finish or the abandoned session to expire, then inspect the target before retrying.
- Use an exclusive batch for a change that must not be interleaved with another controller.

Detailed controller listing is non-waking. It is part of the intended public tool surface but is not present in every preview build.

## Jobs, timeouts, and cancellation

A Convoy tool call can return before the remote work finishes. Keep its delivery/job ID and use `convoy_get_job` to reconcile it after a timeout or reconnect. A timeout means "the client stopped waiting," not "the operation definitely did not run."

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

[Owlette](https://owlette.app) is not required for Convoy LAN communication. In the current preview, its read-only inventory and status bridge uses an `OWLETTE_API_KEY` available to the Convoy host app process. After configuring the host environment, restart the host app and call `convoy_owlette` with `action=capabilities` to verify the connection. `OWLETTE_SITE_ID` may be set as an optional default.

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

**Repair Convoy App** is also the repair action. It should preserve the computer's Convoy identity and durable records while refreshing the installed runtime.

**Stop Convoy App** intentionally prevents its login supervisor from immediately starting it again. Existing queued job records remain. Press **Start Convoy App** to resume.

**Uninstall Convoy App** removes the background app and login registration but retains the local host identity, job history, the host log, and the dedicated Convoy runtime venv when one was built (macOS always builds one; Windows now prefers a per-user one too), so a later reinstall rejoins as the same host and reuses the already-built runtime without a rebuild. Incomplete or unrecognized payload directories are named but never deleted. The confirmation names each retained path before anything is removed. Explicitly saved project artifacts are project files and are not part of host-app cache cleanup.

## The Windows daemon interpreter (no console window at logon)

On Windows the host app is launched at every logon by a Scheduled Task
pointing at `pythonw.exe` -- the *windowless* half of the per-user Convoy
venv. Some versions of `uv` (0.11.x and earlier) wrote that file as a
console binary anyway, byte-identical to `python.exe`
([astral-sh/uv#19226](https://github.com/astral-sh/uv/issues/19226), fixed
in uv 0.12.4). The symptom is an empty terminal window that appears at
every login and does nothing. A newer `uv` fixes newly built environments
and nothing about the one already on your machine, so Embody checks and
repairs it directly.

**What Install and Repair Convoy App now do.** Every install checks what
the daemon interpreter actually is, not what it is named. If it is a
console binary, Embody replaces it in place with the windowless
interpreter from the same base Python -- it never rebuilds the
environment for this, because the environment is otherwise healthy and a
rebuild would try to delete a file the running daemon is holding open.
The `python.exe` beside it is deliberately left alone: package installs
are driven through that one and it is supposed to be a console binary.

**About the `pythonw.exe.old-...` file.** Windows cannot delete a program
that is running, but it can rename it, so a repair over a live daemon
leaves the previous interpreter beside the new one under a name ending in
`.old-` and says so in the install details. The next Install or Repair
removes it, once the old daemon has exited and the environment has a
healthy interpreter again -- deliberately not before, because in the rare
case where the current `pythonw.exe` is missing, that renamed file is the
only copy left. It is safe to leave alone.

**What a refused repair does and does not change.** The guarantee is
about one file: a refusal never alters the `pythonw.exe` your login task
launches. A repair that has to copy several files can be interrupted part
way and leave support DLLs beside a still-unrepaired interpreter; those
are inert -- nothing loads them until a matching interpreter is in place
-- and the next successful repair overwrites them.

**Details you may see, and what each one means:**

| Reported reason | Meaning and next action |
|---|---|
| `daemon_venv_repair_source_missing` | The base Python behind the Convoy environment could not be found, or it ships neither a windowless interpreter nor the DLLs one needs. The interpreter was not changed. Install Python 3.11+ from python.org, then run **Repair Convoy App**. |
| `daemon_venv_repair_locked` | The running daemon is holding the interpreter, so it could not be replaced. Your `pythonw.exe` is unchanged (support files may have been staged beside it; they are harmless). Use **Stop Convoy App**, then **Repair Convoy App**, then **Start Convoy App**. Two variants say more: *"could not be read back either ... unverified"* means another program had the file open and nothing about a console window was actually established -- just repair again later; *"no longer exists"* means the environment lost its interpreter entirely, and **Repair Convoy App** rebuilds it. |
| `daemon_venv_repair_unsafe_path` | A file inside the Convoy environment's `Scripts` folder resolves outside it (a junction or link). Embody refuses to write through it. Remove the link, or uninstall and reinstall the host app. |
| `daemon_venv_not_windowless` | The replacement was written, was read back successfully, and still reports itself as a console program -- so the base Python this machine used supplies a `pythonw.exe` that is not windowless. Run **Repair Convoy App** after installing a stock Python 3.11+ from python.org; if it repeats, report it with the install details. |
| `runtime_interpreter_not_windowless` | A **managed Convoy Runtime bundle** names a console binary as its daemon interpreter. Embody refuses to install or trust it rather than repairing it: a managed runtime is signed and hash-pinned, so it can only be fixed by a corrected release. Report the release version. |

Windows install records also carry a diagnostic `interpreter_subsystem`
field (`gui`, `console`, or `unknown`) in the per-user
`installed.json`. It is informational -- it lets a support answer say
whether a window will appear without asking anyone to watch a login --
and it is absent on macOS, which has no such distinction.

## Platform validation status

Convoy is designed for Windows x64 and Apple Silicon macOS in every direction: Windows/Windows, Windows/macOS, macOS/Windows, and macOS/macOS. Cross-platform behavior is a release requirement, but design intent and automated tests are not the same as hardware acceptance.

At the time of this preview:

- Production runtime packaging is not complete in every build.
- Windows-to-Windows physical acceptance is in progress.
- On macOS, host-app install and the daemon runtime are validated on Apple Silicon hardware; login supervision, the firewall flow, and node-to-node LAN operation have not yet completed physical acceptance.
- Intel macOS is not the first supported Convoy host target.

Consult the release notes for the combinations certified by the build you plan to deploy.

## Related guides

- [Convoy overview and daily use](index.md)
- [Convoy Parameter Reference](../embody/parameters.md#convoy)
- [Setup Wizard](../embody/setup-wizard.md)
- [Envoy Troubleshooting](../envoy/troubleshooting.md)
