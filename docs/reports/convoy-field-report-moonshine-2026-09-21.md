# Convoy field report: moonshine on TEC-A4D and TEC-C3A (2026-09-21)

What went wrong while getting Convoy to carry work from TEC-A4D (the moonshine
dev machine) to TEC-C3A (the Pro-licensed build machine) on the studio LAN,
20-21 September 2026. Written by the Claude Code session working in the
moonshine repo, from the controller side: TEC-A4D's host-app `audit.jsonl`,
`host.log`, `peers.json` and `realm.json`, moonshine's Embody logs, bridge tool
output, and the TEC-C3A Embody log lines the operator pasted. There was no
shell on TEC-C3A. Times are PDT (UTC-7). No PSK, token or key material is
reproduced. Not in the mkdocs nav; internal working record, like the TDXN
review.

**Verdict: Convoy works now, one way only, after four manual repairs.** Every
fault below was silent where an operator looks. TEC-A4D's **Status** read
`Connected` for the three and a half days it sat alone in a realm of one,
listening on an OpenVPN adapter. It still reads `Connected` while its three LAN
peers reject its certificate about twenty times a minute.

The chain on TEC-A4D:

1. Auto-bind put the LAN listener on the OpenVPN adapter (from 13 Sep).
2. The host id and identity were replaced (18 Sep, cause unknown), so every
   peer's pin went stale.
3. Twenty seconds later a smoke-test project founded a private realm, although
   `peers.json` listed three admitted members of the real one.
4. When the real mesh became reachable, the host logged its members as
   "un-admitted" and latched no conflict.
5. The private realm id reached moonshine's tracked `.embody/project.json` (our
   commit).

On TEC-C3A, Convoy could not install or update its host app because Envoy had
never built the venv it borrows, and it reported that only in a log line.

## Setup

| | TEC-A4D (controller) | TEC-C3A (target) |
|---|---|---|
| Role | moonshine dev checkout, Embody dev project also open | moonshine checkout used for locked release builds |
| Network | `Ethernet 2` (Intel I211), 192.168.88.10/24 DHCP, profile **Private**; `Local Area Connection` (TAP-Windows Adapter V9 for OpenVPN Connect), 10.8.0.2/24 manual, profile **Public**; Tailscale, logged out | 192.168.88.36 |
| Host app | host `2674ec80` from 3 Aug (6.0.182), updated in place through 6.2.55; host `5f33ea85` from 18 Sep; 6.2.59 since 20 Sep 16:17 | 6.0.280, installed by the Control and Render.43 projects |
| moonshine's Embody | 6.2.46, updated to 6.2.59 on 20 Sep | 6.2.59, from the committed `.toe` |
| OS | Windows 11 Pro 10.0.22631 | not inspected |

The rest of the mesh: TEC-B4A (192.168.88.20, node `e1.2`, Embody 6.2.8), a
third host `ed513d74...` (192.168.88.24 on 16 Sep, .46 on 21 Sep), and
TEC-MBA.local nodes (offline).

## Timeline

| When (PDT) | What |
|---|---|
| 13 Sep 08:50 | A4D host app 6.2.51 starts on 192.168.88.10:47600, identity `cvfp1-ap4d-...`. Later in that run, before 15 Sep 02:28: `lan_no_route`, then a rebind to **10.8.0.2** |
| 15-16 Sep | Restarts on 6.2.52 through 6.2.55 all bind 10.8.0.2 |
| 16 Sep, after 01:48 | Listener back on 192.168.88.10 |
| 16 Sep 23:17 | A4D admits TEC-B4A, TEC-C3A and `ed513d74...` by `lan_tofu`, all in realm `cv_373a3f415ef5c8bb`. They pin A4D as `cvfp1-ap4d-...` |
| 18 Sep 01:25:03 | Convoy consent re-recorded (`consent.json`) |
| 18 Sep 01:25:10 | Host app 6.2.56 starts with a **new host id** (`5f33ea85...`) and a **new identity** (`cvfp1-fk90-...`), on 10.8.0.2 |
| 18 Sep 01:25:30 | A project in `%TEMP%\embody_smoke` registers: realm candidate `cv_0d864df1f2630c74`, 0 peers |
| 18 Sep 01:25:45 | `cv_0d864df1f2630c74` established, generation 1 |
| 18-20 Sep | 30 `relay_refused` for target `'host-remote'`, a unit-test fixture |
| 20 Sep 16:17 | `smoke_run.py` (`embody-smoke\win32-20260920-161544`) updates the host app in place to 6.2.59 |
| 20 Sep 20:05 | moonshine updated to Embody 6.2.59; **Enable Convoy** on. Status `Connected`; the mesh is this machine alone, peer address 10.8.0.2. The binding is committed (moonshine `8b526ab`) and pushed at 20:51 |
| 21 Sep 16:22:36 | `lan.json` pins `bind` to 192.168.88.10; live rebind |
| 21 Sep 16:22:37 | First inbound `peer_handshake_refused` (`TLSV1_ALERT_UNKNOWN_CA`) |
| 21 Sep 16:22:38 | Three `realm_foreign_advisory`: the admitted peers logged as "un-admitted" and ignored |
| 21 Sep 16:37 | Operator disconnects OpenVPN |
| 21 Sep 16:37:59 | The Embody dev project on A4D, bound to `cv_373a3f...`, is refused: `local_realm_conflict` |
| 21 Sep 16:39:58 | Operator runs **Resolve Realm Conflict** -> **Join Other Realm** in moonshine; `cv_373a3f...` adopted |
| 21 Sep 16:41 | moonshine `e24edd8` rebinds the tracked `project.json`; pushed |
| 21 Sep 16:45 | C3A pulls and opens moonshine: no interpreter, host-app update fails, node registers with "Envoy port pending" |
| 21 Sep 16:46-17:20 | From A4D: `TEC-C3A / moonshine` is `offline` with `last_seen_age_s` 1-7, `limited` |
| 21 Sep 17:17 | **Repair Convoy App** on C3A: same failure |
| 21 Sep ~17:20 | Operator enables Envoy on C3A; node `online` at 17:20:58, still `limited` |
| 21 Sep 17:35 | 1,570 inbound refusals since 16:22:37, still about 20 a minute |

## Findings

In order of cost.

### 1. Auto-bind chose the OpenVPN adapter, and nothing said so

With no `lan.json`, 6.2.59 binds for enabled membership on the automatic
interface (`desired_lan_endpoint`, `start_lan_if_configured`). `host.log`:

```
convoy LAN: peer listener on 192.168.88.10:47600 (pinned mutual TLS; identity cvfp1-ap4d-b2r7-h53v-t9t7-w6ha-5v6r-xb3y-c951)
convoy LAN: lan_no_route: could not determine a routable IPv4 interface to bind (only loopback/link-local found). Set an explicit 'bind' in lan.json to name the interface
convoy LAN: peer listener on 10.8.0.2:47600 (pinned mutual TLS; identity cvfp1-ap4d-b2r7-h53v-t9t7-w6ha-5v6r-xb3y-c951)
```

After that, the listener and the 47601 discovery socket sat on the TAP adapter
for most of 13-21 September. On 21 Sep, `netstat` showed
`TCP 10.8.0.2:47600 LISTENING` and `UDP 10.8.0.2:47601`, both pid 39812.

**Why.** `convoy_lan.py` says VPN and virtual adapters are excluded, but the
interface comes from the no-packet route probe, and the OS routed the probe
through the TAP adapter. The only `0.0.0.0/0` route was via `Ethernet 2`
(metric 25), so OpenVPN presumably pushed more specific routes (its usual
`0.0.0.0/1` and `128.0.0.0/1`). We did not capture the route table while it was
connected. 10.8.0.2 is RFC 1918, so an address-range exclusion cannot catch it.

**Exposure.** Windows classed that network **Public**. The Convoy docs say
never to enable it on public networks, but auto-bind does not check the
network category. An inbound allow rule for `...\Python311\pythonw.exe`, the
interpreter that owns the socket under the `runtime-venv` launcher, covered
Private and Public, so the listener was reachable from the VPN.

**What the operator saw.** `Convoystatus: Connected`. The only clue was a peer
row reading `ip=10.8.0.2 local=True` in `convoy_list_nodes`.

**Repair.** `%LOCALAPPDATA%\EmbodyConvoy\lan.json` set to
`{"enabled": true, "bind": "192.168.88.10"}`. The host rebound live
(`lan_endpoint_changed`, 10.8.0.2 -> 192.168.88.10, 16:22:36), no restart
needed. That was the most effective single step. It pins a DHCP address, so it
is a stopgap.

This also exposed two smaller problems:

- `host.log` lines have no timestamp except the start banner, so the switch to
  the VPN cannot be dated closer than "between 13 Sep 08:50 and 15 Sep 02:28".
- The `convoy_lan.py` module docstring still says an absent `lan.json` means
  "NO LAN SOCKET, EVER ... the state of every shipped build". 6.2.59 does the
  opposite. We read the docstring first and lost time on it.

### 2. The host identity was replaced, and every LAN peer now rejects this machine

`audit.jsonl` on A4D:

```
2026-09-18 01:25:10 hoststore host_id_minted  {"host_id": "5f33ea85f83e00ebf44dd0d84df9d777"}
2026-09-18 01:25:10 hostkeys  identity_minted {"fingerprint": "cvfp1-fk90-ksq9-w1k7-35xn-weym-9rjk-5ffv-p6q8", ...}
```

From 3 Aug until then, `host.log` shows one host, `2674ec80`, with identity
`cvfp1-ap4d-b2r7-h53v-t9t7-w6ha-5v6r-xb3y-c951`. `consent.json` was re-recorded
at 01:25:03. `host.json`, `host.token`, `identity.key` and `identity.cert.pem`
are all dated 18 Sep 01:25. `peers.json`, `audit.jsonl` and `logs\host.log`
survived. The interpreter changed at the same moment, from uv's `cpython-3.11`
to `runtime-venv` over `Python311` (visible in the traceback paths).

**Cause: not determined.** `convoy_hoststore._load_host_file` and
`convoy_hostkeys` mint only on `FileNotFoundError`, and uninstall keeps
`host.json` and `host.token` (`RETAINED_NAMES`). So the files were gone before
the 6.2.56 start, and something outside those code paths removed them.
Neither `host.log` nor the audit log records an uninstall or reset, and the
Embody dev-project logs for 18 Sep contain no Convoy lines. Worth tracing: what
removes `host.json`, `identity.*`, `host.token` and `consent.json` but leaves
`peers.json`?

**What it broke.** The three LAN peers pinned `cvfp1-ap4d-...` on 16 Sep. Once
A4D was back on the LAN, every connection they made to it was refused:

```
2026-09-21 16:22:37 peer_handshake_refused {"detail": "SSLError:TLSV1_ALERT_UNKNOWN_CA", "source": "192.168.88.36"}
```

There were 1,304 of these from 16:22:37 to 17:22 (in the last ten minutes of
that window, 146 from .36, 59 from .20 and 40 from .46), 1,570 by 17:35, and
they continue. The alert comes from the connecting peer, so C3A, B4A and
`ed513d74...` are rejecting the certificate A4D presents now. The other
direction works: from A4D, `convoy_ping` to C3A returns `pong: true` and a
`convoy_call` job was delivered and queued. The mesh is half up, and the half
that fails is the one nobody is watching. We could not read C3A's `peers.json`,
so the stale pin is inferred from the alert and the dates.

**What the operator saw.** Nothing. A4D's Status reads `Connected`,
`convoy_list_nodes` shows the peers `online`, and the refusals exist only in
`audit.jsonl`. `docs/convoy/host-app.md` has no procedure for re-pinning a host
whose identity changed. The realm join in finding 4 did not help, because this
is a pin problem, not a realm problem.

**Not yet repaired.**

### 3. A test project founded a private realm while the host knew the real one

Twenty seconds after the new identity:

```
2026-09-18 01:25:30 realm_candidate       {"convoy_id": "cv_0d864df1f2630c74", "generation": 0, "source": {"hostname": "TEC-A4D", "project_root": "C:\\Users\\admin\\AppData\\Local\\Temp\\embody_smoke", "via": "register"}}
2026-09-18 01:25:30 peer_sessions_started {"namespaces": ["cv_0d864df1f2630c74"], "peers": 0}
2026-09-18 01:25:45 realm_established     {"convoy_id": "cv_0d864df1f2630c74", "generation": 1, ...}
```

`%TEMP%\embody_smoke` is the hand-run directory that
`dev/release_testing/smoke_bootstrap.py` documents. The smoke template's feature
phase does "a real Convoy enable-to-Connected" and auto-answers
`Embody - Enable Convoy` (`SMOKE_CONVOY_RESPONSES`), against the developer's own
`%LOCALAPPDATA%\EmbodyConvoy`. The smoke's isolation covers the working
directory, not the per-user host app.

Other tests reach the same host:

- `smoke_run.py`. On 20 Sep 16:17, `installed.json` records
  `installed_by: ...\Temp\embody-smoke\win32-20260920-161544 (/Embody)`: the
  in-place update to 6.2.59.
- `test_envoy_bridge.py`. The audit log holds 30 `relay_refused`
  (`'host-remote' is not a host_id`) between 18 and 20 Sep. `host-remote` is a
  fixture in `dev/embody/unit_tests/test_envoy_bridge.py`, so at least one bridge
  test sends real relays to the live host.

**Why the realm was private.** The host was listening on the VPN, so the 15 s
listen window heard nothing. At that moment `peers.json` held three hosts in
state `admitted`, each with `convoy_ids: ["cv_373a3f415ef5c8bb"]`, admitted 26
hours earlier. The host founded a new realm anyway.

**What it broke.** For three and a half days, every project on A4D belonged to
`cv_0d864...`, a realm with no other member.

### 4. When the real mesh became visible, its members were logged as strangers

As soon as `lan.json` moved the listener to the LAN:

```
2026-09-21 16:22:38 realm_foreign_advisory {"detail": "un-admitted LAN host advertises a foreign established Convoy; ignored (realm is committed). Use Resolve Realm Conflict / the denylist if it should be silenced.", "realm_states": {"cv_373a3f415ef5c8bb": "established"}, "sender": {"address": "192.168.88.36:47600", "fingerprint": "cvfp1-t9zg-nbcx-ydhr-fxaw-15xx-c7fj-jwjh-m7v7", "host_id": "72a18b02285720c56a28dbfeff2435f4"}}
```

The same entry was logged for .20 and .46. All three senders are `admitted` in
this host's own `peers.json`, with the same host ids and fingerprints.
`realm.json` kept `conflict_ids: []`, and moonshine's Status still read
`Connected` at 16:39:47.

A conflict first surfaced at 16:37:59, and only because the Embody dev project
on the same machine, bound to `cv_373a3f...`, tried to register:
`register_refused`, `local_realm_conflict`. The documented recovery then
worked. The operator ran **Resolve Realm Conflict** -> **Join Other Realm** in
moonshine at 16:39:58 (`realm_adopted`, via `operator_adopt`), and the mesh
appeared: TEC-B4A / e1.2, TEC-C3A / Control and TEC-C3A / Render.43 online, the
rest offline.

### 5. The private realm id was committed to a tracked file

Enabling Convoy on moonshine on 20 Sep logged:

```
20:05:38 INFO    embody_admin:2780: Recorded convoy cv_81260fa38d885de8 in .embody/project.json (consent scope: trusted LAN Convoy mesh). It is a TRACKED file, so every clone of this repo shares the convoy.
20:05:38 SUCCESS ConvoyExt: Convoy: enabled for this project: convoy cv_81260fa38d885de8 (consent already given on this install)
```

Eighteen seconds later, `.embody/project.json` held
`"id": "cv_0d864df1f2630c74", "binding_state": "established"`. The host's
realm replaced the minted id and nothing logged the substitution, so the
SUCCESS line names an id that was never persisted. We committed the file as it
stood (`8b526ab`) and pushed it. Checking the realm before committing it was
our job, and we did not do it.

**What it broke.** Every clone of moonshine carried a binding to a
one-machine realm, which C3A's LAN would refuse. Fixed by `e24edd8` after the
join.

**Embody-side point.** When it wrote the tracked file, Embody had what it
needed to warn: the realm had no peers, and admitted peers of a different
established realm were on record.

### 6. On C3A, Convoy needed Envoy's venv and said so only in a log line

C3A's moonshine checkout (`C:/Users/dylan/Documents/Git/moonshine`) had never
run Envoy, so `touch\.venv` did not exist. C3A's host app was 6.0.280. After
`git pull`, the operator opened moonshine (Embody 6.2.59) and turned on
**Allow Execute TD Python** and **Allow Full Shell**. C3A's Embody log, in C3A
local time:

```
16:45:46 WARNING ConvoyExt: Convoy: no usable interpreter in C:/Users/dylan/Documents/Git/moonshine/touch\.venv\Scripts -- Convoy cannot start its host app (looked for python, python3, python3.x)
16:45:46 INFO    ConvoyExt: Convoy: Registered -- Envoy port pending
16:45:47 INFO    ConvoyExt: Convoy: Convoy safety-policy change requested (Convoyallowtdpython); the parameter shows the approved value until the host accepts
16:45:53 INFO    ConvoyExt: Convoy: Convoy safety-policy change requested (Convoyallowfullshell); the parameter shows the approved value until the host accepts
16:45:56 INFO    ConvoyExt: Convoy: Convoy App update: the running Convoy App is 6.0.280, this Embody ships 6.2.59 -- updating it in place now (automatic; Repair Convoy App remains the manual path)
16:45:56 WARNING ConvoyExt: Convoy: no Convoy runtime is available -- no signed managed runtime, and no usable interpreter at '<no venv path>'. Enable Envoy (it builds the Python environment Convoy shares) and the host app installs itself.
16:46:44 INFO    ConvoyExt: Convoy: host app answered registration -- replacing the stale host-app line (Install failed -- see log)
17:17:53 WARNING ConvoyExt: Convoy: no Convoy runtime is available -- (same text, after Repair Convoy App)
17:17:56 INFO    ConvoyExt: Convoy: host app answered registration -- replacing the stale host-app line (Install failed -- see log)
```

From A4D, 16:46-17:20: `TEC-C3A / moonshine` had `status: offline`,
`online: false`, `last_seen_age_s` between 1 and 7, and
`compatibility: limited`. `convoy_ping` returned `pong: true, online: false`. A
`get_td_info` sent with `convoy_call` stayed `queued`. The permission toggles
showed on but were not in effect. Repair changed nothing, since there was still
no interpreter. The operator found the cause (Envoy had never been
initialized), enabled it, and the node came online at 17:20:58.

Embody-side points:

- Enable Convoy neither builds the runtime it needs nor refuses with a reason.
  The setup steps in `docs/convoy/index.md` do not mention Envoy, and the one
  actionable sentence ("Enable Envoy ...") is a log WARNING.
- Status showed `Install failed -- see log`, and the next registration
  **replaced** it, so the failure vanished from the one place a person looks.
- `offline` covered "registered, heartbeating, no Envoy relay port yet". A node
  that answers pings every few seconds is not offline, and a controller cannot
  see the real state.
- `smoke_bootstrap.py` already records the same failure on a clean machine
  ("no interpreter -- Envoy was still building the venv Convoy shares").
- moonshine's tracked `project.json` has declared `envoy.enabled: true` since
  20 Sep, and `docs/envoy/setup.md` says a machine with no settings of its own
  turns Envoy on at first open. C3A's checkout was not fresh, so its local
  `.embody/config.json` probably overrode the declaration. Not verified.

### 7. The controller could not answer "why"

- Every C3A node reports `compatibility: limited`, including the 6.2.59 node
  after it came online, with no reason. The remote host app's version is not in
  `convoy_list_nodes`, `convoy_ping` or `get_convoy_status`, which reports only
  the local `app_version`. We had to ask the operator to read C3A's log to learn
  it was 6.0.280. A4D logged `peer_http_compat_fallback` (`peer.nodes`) for all
  three peers at 16:39:58, which suggests host-app skew. Whether C3A's host app
  has since updated to 6.2.59 is unknown.
- The bridge's `Connected to Envoy` line names a port, not a project. With the
  Embody dev project on 9870 and moonshine moved automatically to 9871 (later
  9872, as instance `moonshine.3-2`), we attached to the wrong project once. We
  later got `Envoy connection lost` until each call passed `instance`.
- Along the way (Envoy, not Convoy): `set_parameter` on a pulse parameter
  (`Checkforupdate`) returned `success: true` and did nothing. Updating Embody,
  a prerequisite here, took `par.Checkforupdate.pulse()` through
  `execute_python`.

### Minor

- A firewall rule named `Convoy tracer (temporary)` (group `Embody`, TCP 9891,
  all profiles including Public, any program) is still enabled on A4D. Nothing in
  the Embody repo creates it, so it is probably left over from a debugging
  session.
- Pulsing **Resolve Realm Conflict** with nothing to resolve (16:40:03, five
  seconds after the join) opened no dialog and logged nothing.

## Where it stands

| | State |
|---|---|
| A4D -> C3A | Works: ping, node list, job delivery |
| C3A, B4A, `ed513d74...` -> A4D | Refused (`UNKNOWN_CA`), about 20 a minute |
| Realm | `cv_373a3f415ef5c8bb` on both machines; moonshine's tracked binding matches (`e24edd8`) |
| A4D bind | Pinned to 192.168.88.10 by `lan.json`, which is a DHCP address |
| C3A moonshine node | Online, `limited`. Unverified whether TD Python and Full Shell are now in effect |
| Firewall | `Convoy tracer (temporary)` still enabled |

## Recommendations

1. **Auto-bind.** Identify tunnel adapters by interface type and description
   (TAP-Windows, Wintun, WireGuard, OpenVPN), not by address. Refuse to
   auto-bind an interface on a Windows Public network. Show the bound interface
   in Status whenever it changes, for example "LAN: 10.8.0.2 (OpenVPN TAP) --
   set bind in lan.json".
2. **Genesis.** Do not found a realm while `peers.json` holds admitted members
   of an established realm; wait for them or ask. When admitted peers advertise
   a different realm, latch a conflict and show it in Status instead of logging
   an advisory about "un-admitted" hosts.
3. **Identity.** Make a re-mint loud: show it in Status and warn that N pinned
   peers will now reject this host. Give the peer side a supported re-pin
   action, and document it in `host-app.md`. Find what removed `host.json` and
   `identity.*` on 18 Sep.
4. **One-way failures.** Count inbound handshake refusals and report them in
   Status and `get_convoy_status`, for example "3 peers reject this host's
   certificate".
5. **Test isolation.** Point the smoke run (hand-run and `smoke_run.py`) and
   `test_envoy_bridge.py` at a throwaway Convoy data directory, the way
   `convoy_hardware_e2e.py` already accepts `data_dir`, or refuse to run on a
   machine with a live host app.
6. **Tracked binding.** Before writing a realm id to `project.json`, check that
   the realm has a peer or that no admitted peer disagrees. Log the id actually
   written.
7. **Convoy needs Envoy.** Have Enable Convoy build or require the runtime, with
   a dialog that says so. Keep `Install failed` in Status until it is fixed. Add
   the requirement to the setup steps. Report "registered, Envoy port pending"
   to controllers instead of `offline`.
8. **Diagnostics.** Give `compatibility` a reason and both versions. Put the
   remote host-app version in `convoy_list_nodes`. Timestamp every `host.log`
   line. Have the bridge name the project it connected to.
9. **Docs.** Correct the `convoy_lan.py` docstring on an absent `lan.json`.

## Our own errors

Listed so the findings above can be weighed:

- We first told the operator Convoy did not exist, after searching only the
  moonshine repo.
- We saw the 10.8.0.2 peer address on 20 Sep, flagged it, and did not
  investigate until the operator pushed a day later.
- We committed and pushed the private realm id despite Embody's own
  "TRACKED file" warning (finding 5).
- We first diagnosed the `UNKNOWN_CA` refusals as the realm split. The join
  fixed the split and not the refusals.
- On 20 Sep we mistook the installed product
  (`C:\ProgramData\Moonshine\Moonshine.toe`, which serves the same ports and has
  no Embody) for the dev checkout, and gave the operator steps for it. No write
  reached it.
