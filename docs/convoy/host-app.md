# The Convoy Host App

The **host app** is the other half of [Convoy](index.md): a small program that runs outside TouchDesigner, keeps the register of this user's Convoy nodes, holds the durable job queue, and relays audited operations into a node through its Envoy server.

!!! warning "Built, but never yet installed end to end — treat it as unproven"
    The install path now exists. The `.tox` carries the host app itself (as text DATs, so an install needs no network), and the Convoy page has **Install or Update Host App**, **Start**, **Stop** and **Uninstall Host App**. Pressing Install asks for confirmation, writes the daemon into your per-user data directory, and registers it to start when you log in.

    **What has not happened is a single complete install on real hardware.** The orchestration is covered by tests — including a run inside TouchDesigner — and the daemon has been started from an installed payload and answered `/health`. But the end-to-end acceptance run (install → supervised restart → survives logout and reboot → uninstall → reinstall rejoins the same convoy) has not been performed. Until it has, treat the buttons as unproven rather than supported, and know that [Uninstall](#how-the-install-behaves) removes the program and its login entry while keeping your host identity and job history.

    Two specific limits, stated plainly rather than discovered later:

    - **macOS is entirely unverified.** The Launch Agent, the plist and the `launchctl` calls are generated and unit-tested, but no part of the macOS path has ever run on a Mac. Only the Windows leg has been exercised at all.
    - **A host installed this way has no cryptographic identity.** TouchDesigner bundles Python 3.11.15 without the `cryptography` package, so the daemon starts and serves normally but reports `cryptography_missing` and holds no signing key. That is harmless for the loopback-only feature that exists today, and it is a hard blocker for the LAN peering described under [What is coming](#what-is-coming) — peer admission is built on pinned key fingerprints, and there is no key to pin.

    The program it installs is **unsigned** and lives in a user-writable directory, which is by construction a persistence mechanism; security software may reasonably flag it. It is **one per logged-in user**, not one per machine.

## Why it lives outside TouchDesigner

The host app is machine-scoped — more precisely, **per logged-in user** — and the node is per project. Putting the coordinator inside one of the things it coordinates does not work:

- **It has to survive TD being closed.** A project is still a node when its TouchDesigner is not open; the host app can still answer for it, list it, and hold work queued for it. When TD opens again, the node re-registers and its Envoy port comes back.
- **Many projects, one register.** Several projects on one machine each register as their own node with the same host app. Nothing about that arrangement fits inside any one of them.
- **Durable jobs must outlive a session.** A job is persisted before it is acknowledged, and its record survives a host restart. A queue that dies with a `.toe` is not a queue.
- **A relay must not be reachable only while its target is up.** The whole point is to have somewhere to ask about a project that is not currently running.

## What it does when it runs

- Binds `127.0.0.1` on an **OS-assigned port** and writes `host.portfile.json` with that port and its own pid. Nothing binds off the machine.
- Mints a **host id** once, host-private, and serves it on `GET /health` — the only unauthenticated route. Every other route requires the `X-Convoy-Host-Token` header, read from a 0600 file in the same directory.
- Keeps the **node register**: TD sessions register their project root, Embody COMP path, convoy id, per-launch runtime id, and Envoy port, and get a host-minted node id back.
- Turns an authorized request into a **durable job** — persisted before it is acknowledged — and records every state change in an append-only audit trail.
- Optionally **drains itself**: with `--drain-interval N` it polls running node jobs and dispatches queued ones every N seconds. **This is off by default** (`0`), which means a host app started with no arguments never dispatches on its own; work moves only when something asks it to.
- Refuses to be run twice against the same data directory. An exclusive `host.lock` is taken before anything else, and a second instance exits with a clear line rather than corrupting the first one's in-flight job claims.

Clients never trust a portfile on its face: the writing process's liveness is checked, and the host app's identity is confirmed through the unauthenticated `/health` route **before** the IPC token is ever transmitted. A recycled port therefore reads as `Host app stale`, not as a live host to hand a credential to.

## Where it keeps its state

One per-user directory, never inside your project and never in git:

| Platform | Directory |
|---|---|
| Windows | `%LOCALAPPDATA%\EmbodyConvoy` (Local, never Roaming — identity must not follow a roaming profile to another machine) |
| macOS | `~/Library/Application Support/EmbodyConvoy` |
| Linux | `$XDG_STATE_HOME/embody-convoy`, or `~/.local/state/embody-convoy` |

| File | Contents |
|---|---|
| `host.json` | Host identity and the node register. Rewritten whole, atomically, and only on register / approve / remint |
| `host.token` | The per-install IPC token (0600). Created by the host app; nothing else ever mints one |
| `host.portfile.json` | The current port and the pid that wrote it. Cleared on a clean shutdown |
| `host.lock` | The singleton lock, held for the process lifetime. A crash releases it; a pid file would not |
| `jobs/` | One JSON file per job. A job write never rewrites the others |
| `audit.jsonl` | Append-only, one JSON object per line |

## What to expect today

| Your situation | What you see |
|---|---|
| Embody installed, **Convoy Enable** off (the default) | `Disabled`. Convoy does nothing and costs nothing |
| **Convoy Enable** on, no host app on the machine | `No Convoy host app`, indefinitely. One DEBUG line on the transition, a slower tick, and no error, no dialog, no retry storm |
| **Convoy Enable** on, project never saved | `Waiting for project save` |
| A source checkout of the Embody repo, daemon started by hand | The node registers within a few seconds and the status becomes `Registered <node> (host <host>)` |

## Running it from a source checkout

!!! danger "Developer path — not a supported install"
    This is how the host app is exercised during development. It is not an install: nothing supervises the process, nothing restarts it, it does not survive a logout, and closing the terminal ends it. Do not treat it as a way to deploy Convoy.

From a clone of the [Embody repository](https://github.com/dylanroscover/Embody) (the daemon is stdlib-only Python 3, except that it will use `cryptography` for its host identity key if the interpreter you run it under happens to have it — TouchDesigner's does not):

```bash
python dev/convoy/convoy_hostapp.py
```

It prints the host id, the loopback port, and its data directory, then serves until interrupted. Useful flags:

| Flag | Effect |
|---|---|
| `--data-dir DIR` | Use a scratch state directory instead of the per-user one — the safe way to experiment |
| `--port N` | Pin the loopback port instead of letting the OS assign one |
| `--drain-interval N` | Poll running node jobs and dispatch queued ones every N seconds. Off (`0`) unless you pass it |
| `--no-singleton` | Start even if another host app holds the data directory. **Tests only** — two daemons on one data directory burn each other's live job claims |

If a TouchDesigner project on the same machine has **Convoy Enable** on, it will find this host app on its next reconcile tick and register with it. That is the intended behaviour, and it is worth knowing before you start one.

## How to tell it is working

**From TouchDesigner** — the Convoy page on the Embody COMP:

- **Convoy Status** reads `Registered <node> (host <host>)`. That is the steady state.
- `Registered -- Envoy port pending` means the registration landed but Envoy has not bound its port yet; it resolves on its own within seconds.
- `No Convoy host app` means the probe found nothing. Nothing is broken.

**From outside TouchDesigner** — in the data directory above:

- `host.portfile.json` exists and names a pid that is actually running.
- The health route answers, unauthenticated, with the same host id:

```bash
curl http://127.0.0.1:<port>/health
# {"ok": true, "protocol": "convoy-host/1", "host_id": "..."}
```

Every other route is authenticated:

```bash
curl -H "X-Convoy-Host-Token: <contents of host.token>" \
     http://127.0.0.1:<port>/status
```

`/nodes` lists the registered nodes, and `/jobs/<id>` reads one job record.

## Stopping it

- **Interrupt it** (Ctrl+C, or `SIGTERM` / `SIGINT` / `SIGBREAK`). The daemon unwinds cleanly: it stops the drain loop, clears the portfile, writes a `stopped` audit line, and releases the lock.
- **Or ask it to stop** over the authenticated `POST /shutdown` route, which runs the same path.
- **A hard kill** leaves the portfile behind. Nothing breaks: clients verify the writer's pid, so a dead port is never handed out as live — the status simply reads `Host app stale`.

## How the install behaves

These are properties of the install that now exists, not promises.

- **Its own confirmation, separate from Convoy Enable.** Installing a program that runs whenever you are logged in — whether or not TouchDesigner is open — is a different grant from registering a project, so it is asked for separately. **Enabling Convoy never installs anything**, and neither does the setup wizard.
- **Unsigned and un-notarized**, dependent on TouchDesigner's bundled interpreter (so moving or uninstalling TD needs a repair — press Install again), **per user, not per machine**, loopback only with no firewall rule created or needed, and never elevated.
- **Install is also repair and upgrade.** Running it again over a broken or older install is the supported fix; it does not need an uninstall first.
- **Asymmetric self-heal, by construction**: Windows Task Scheduler's repetition means a dead daemon comes back within about a minute, while launchd's `KeepAlive` restarts it in about a second. That difference is a property of the two supervisors, not something Convoy hides.
- **Stop really stops.** Because the supervisor would otherwise bring the daemon back within a minute, Stop disables the supervision entry first — otherwise the button would look broken.
- **Uninstall keeps the evidence.** It removes the program files and the supervision entry and deliberately **keeps** `host.json`, `host.token`, `host.portfile.json`, `audit.jsonl` and `jobs/`. A re-install therefore rejoins the same convoy under the same host identity rather than becoming a new host. Uninstall shows you exactly what it will remove and what it will keep before it does anything.

## What is coming

!!! note "Planned, not shipped — none of this exists in this build"
    Described here so the gap between what shipped and what was designed is visible, not to imply it is available.

- **LAN peering between machines.** Everything today is loopback only. Multi-machine Convoy needs a transport, mutual authentication and an admission model, and it is gated on the identity problem noted at the top of this page: a host installed under TouchDesigner's interpreter has no signing key, and peer admission is built on pinned key fingerprints.
- **A verified install.** The acceptance run described at the top — install, supervised restart with a new pid, survives logout and reboot, uninstall, reinstall rejoining the same convoy — has not been performed on real hardware, and none of the macOS path has run on a Mac at all.

## Platform honesty

The host app and its TouchDesigner-side libraries are pure-Python and run their test suites on both **windows-latest** and **macos-latest** in CI. That covers path computation, the probe decision tree, the job store, dispatch, and the protocol.

It does not cover the parts that only a real machine can exercise. The Scheduled Task XML and the LaunchAgent plist are generated and asserted against golden fixtures, and the supervisor commands are tested against captured `schtasks` and `launchctl` output — but a fixture is not a supervisor. **No install or login-supervision behaviour has been verified on a Mac**: that leg is verified by construction and CI only, and is stated that way deliberately rather than being left for a user to discover. The Windows leg has had its unelevated task-registration path exercised from inside TouchDesigner, but not a full install-to-reboot run.
