# The Convoy Host App

The **host app** is the other half of [Convoy](index.md): a small program that runs outside TouchDesigner, keeps the register of this user's Convoy nodes, holds the durable job queue, and relays audited operations into a node through its Envoy server.

!!! warning "Not installable yet — there is no supported install path"
    Nothing in TouchDesigner installs, starts, supervises, or updates the host app today. There is no Install button on the Convoy page, and the shipped `.tox` does not contain the host app at all. **For every user of the released build, turning Convoy on makes [Convoy Status](index.md#status-vocabulary) read `No Convoy host app` — and it will keep reading it.** That is the honest state of this feature: the TD side (a node that registers itself) shipped; the machine side did not.

    The host app currently exists only as source in the Embody repository, under `dev/convoy/`, where it is developed and tested. The installer, the login supervision, and the Convoy-page buttons that would drive them are planned — see [What is coming](#what-is-coming) — and none of it is in this build.

    Everything below marked **not yet available** describes a design, not something you can do.

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

From a clone of the [Embody repository](https://github.com/dylanroscover/Embody) (the daemon is stdlib-only Python 3; `cryptography`, if present, is used solely for the not-yet-active host identity key):

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

## What is coming

!!! note "Planned, not shipped — none of this exists in this build"
    Described here so the gap between what shipped and what was designed is visible, not to imply it is available.

- An **Install** action on the Convoy page that writes the host app into the per-user data directory and registers a **per-user** Scheduled Task (Windows) or LaunchAgent (macOS) to start it at login and restart it if it dies.
- **Its own confirmation, separate from Convoy Enable.** Installing a program that runs whenever you are logged in — whether or not TouchDesigner is open — is a different grant from registering a project, and it will be asked for separately. Enabling Convoy will never install anything.
- The honest properties that install would ship with, stated in advance: **unsigned and un-notarized** (security software may flag an unsigned Python program that runs at login); dependent on TouchDesigner's bundled interpreter, so a TD uninstall or move needs a repair; **per user, not per machine**; loopback only, with no firewall rule created or needed; and never elevated.
- **Asymmetric self-heal, by construction**: Windows Task Scheduler's repetition means a dead daemon comes back within about a minute, while launchd's `KeepAlive` restarts it in about a second. That difference is a property of the two supervisors, and the docs will keep saying so rather than papering over it.
- **Uninstall keeps the evidence.** Removing the host app would delete the program files and the supervision entry, and deliberately **keep** `host.json`, `host.token`, and `jobs/` — the audit trail. Deleting that history would be a separate, second action with its own confirmation, because a re-install afterwards mints a new host identity.

## Platform honesty

The host app and its TouchDesigner-side libraries are pure-Python and run their test suites on both **windows-latest** and **macos-latest** in CI. That covers path computation, the probe decision tree, the job store, dispatch, and the protocol.

It does not cover the parts that do not exist yet. **No install or login-supervision behaviour has been verified on a Mac**, because none of it has shipped. When it does, the shipping note will state exactly which legs were verified on real hardware and which were only verified by construction and CI.
