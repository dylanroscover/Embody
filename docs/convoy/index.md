# Convoy

**Convoy** makes the TouchDesigner projects on one machine addressable as **nodes**. A small program outside TouchDesigner — the [Convoy host app](host-app.md) — keeps a register of which projects exist, which of them are open right now, and where each open one's Envoy server is listening. Anything on the same machine that can authenticate to the host app can then hand it a piece of work, and the host app relays that work into the right project through Envoy.

In this build, every part of that happens over **loopback**. Nothing binds to the network.

!!! info "Convoy today is one machine, and one user on it"
    The host app listens on `127.0.0.1` only, on an OS-assigned port, and there is no non-loopback code path in it. No firewall rule is created and none is needed. A LAN mesh — pinned identities, mutual TLS, per-peer admission — is a later phase that has not been built. The consent Convoy records today grants **`local host app only`**, and widening it will ask you again rather than inherit that grant.

## The three pieces

| Piece | What it is | Where it runs |
|---|---|---|
| **Node** | A TouchDesigner project with **Convoy Enable** on. It registers itself and heartbeats; it never dispatches anything. | Inside TD, as a `ConvoyExt` child COMP on the Embody COMP |
| **Host app** | The per-user register, job queue, and audit trail. Mints the node ids, holds the durable jobs, forwards work into a node's Envoy. | Outside TD, one per logged-in user on the machine |
| **Controller** | Whatever submits work — a script or an AI session on this machine. It talks to the host app, never to a node directly. | Same machine, over loopback |

A node's job is small and honest: reconcile. On a self-adjusting tick — as often as every 4 seconds while something is still settling, as rarely as every 60 when nothing is — `ConvoyExt` computes a desired-state tuple — enabled, project root, Embody COMP path, convoy id, Envoy port, host id — and compares it to what it last sent. **Unchanged, and inside the heartbeat window, costs zero network calls.** Changed, or ~30 seconds elapsed, and it sends one registration. The heartbeat matters because the Envoy port is per-launch and the host app does not persist it: after a host restart, the heartbeat is what puts the port back.

## What enabling Convoy grants

Turning **Convoy Enable** on for a project the first time shows a confirmation naming the id it is about to mint. Cancelling writes nothing at all.

> Convoy gives this project a stable identity so a Convoy host app can find and reach this TouchDesigner session.
>
> Enabling it will:
>
> - mint the convoy id `cv_…`
> - record that id, and this consent, in `.embody/project.json` — a COMMITTED file, so everyone who clones this repo shares the same convoy
> - register this session with the Convoy host app running on THIS machine, over loopback only
>
> Scope granted: this machine's local host app only. Convoy does not reach the network here, and widening that scope later will ask you again.

Stated plainly, in full:

- **A convoy id is written into a committed file.** `.embody/project.json` gains a `convoy` key holding the id (`cv_` plus 16 hex characters), the consent scope `local host app only`, and a UTC timestamp. `.gitignore` deliberately un-ignores that file, so it is committed in every Embody project. **Everyone who clones the repo inherits the id and the recorded consent** — and therefore never sees this confirmation on their own machine. The id is an identifier, not a credential: it names a convoy, it does not grant access to one.
- **This session registers with the host app on this machine, if one is running.** The registration carries the project root, the Embody COMP's path, the convoy id, a per-launch runtime id, and Envoy's port. It is re-asserted about every 30 seconds while TD is open.
- **The host app may relay operations into this project through Envoy** — and only operations in its audited registry (see below). Convoy adds no capability Envoy does not already have locally; it adds a way for something else on this machine to reach it.
- **Anything running as your user on this machine can read the host app's token and send it work.** The token is a 0600 file in your own data directory. It is a boundary against *other users* on a shared machine — not against you, and not against anything already running as you.
- **Turning Convoy Enable off unregisters this node** and clears its port on the host app. The convoy id stays in `project.json` — it is not a secret, and removing it would break the project's identity for every clone. Enabling again does not re-ask, because consent is recorded per project, not per session.

A project that has never been saved to disk refuses to register at all: a node is identified by its project folder, and an unsaved one would mint a throwaway identity. The status reads `Waiting for project save`.

## What Convoy is not

- **Not a render farm.** Nothing schedules, balances, splits, or distributes rendering work. The host app relays individual operations into individual projects.
- **Not remote access.** Nothing in this build binds off the machine. There is no address a second machine could reach, no discovery, and no peers.
- **Not authenticated beyond loopback and a per-user token.** Every route except `GET /health` requires the `X-Convoy-Host-Token` header, read from a 0600 file in the per-user data directory. That authenticates the *OS user*, not the caller. See [Envoy's security model](../envoy/security.md) — Convoy inherits it, because Convoy's relay terminates in Envoy.
- **Not a remote code path.** Operations that execute arbitrary code are refused outright: the TD-Python gate that would make them safe does not exist. Absence from the registry is itself a refusal, and so is an entry that fails to declare its own gating.
- **Not a replacement for Envoy.** Convoy goes *through* Envoy. If Envoy is not running, a node registers without a port and the host app cannot dispatch to it — the status says so explicitly.
- **Not required.** With no host app on the machine — which is every install today — Convoy simply reads `No Convoy host app` and does nothing. That is a resting state, not an error.

## What can be relayed

The host app relays **nothing that is not explicitly entered in its operation registry**. This build ships the seed of that registry:

| Operation | Mutating | Notes |
|---|---|---|
| `convoy_ping` | No | Liveness |
| `query_network` | No | Read the network under a path |
| `capture_top` | No | Cooks the TOP it captures |
| `set_op_position` | Yes | A layout nudge — the benign mutation that exercises the lease gate |
| `run_tests` | Yes | Runs asynchronously; the host mirrors the node's own job handle and polls it to a verdict. `background=True` and `override=False` are forced by the host, so a caller cannot bypass the node's multi-session gate |
| `save_project` | Yes | Also asynchronous; blocks TD's main thread for 15+ seconds and restarts Envoy under itself |

Every field is read with a strict default: an entry that omits one is treated as unaudited, which means refused. Two entries (`run_tests`, `save_project`) additionally carry a flag marking them as never relayable to a remote peer — data recorded now for a phase whose code does not exist yet. Nothing reads that flag in this build.

There is also **no controller application yet**. Nothing ships that submits work to a host app for you; doing it today means speaking the host app's authenticated loopback API directly. Combined with the fact that no host app installs, that is the practical shape of Convoy in this build: a node registers itself correctly, and there is not yet anything on the other end of the register.

## Status vocabulary

**Convoy Status** on the Embody COMP is a read-only readout of this node's registration state, truncated to 160 characters. Every string it can show is listed here. Absence is never reported as an error.

| Status | What it means |
|---|---|
| `Disabled` | **Convoy Enable** is off. Also the resting readout after a disable or a cancelled first enable — this node is off locally whether or not the host app was reachable to hear about it |
| `Waiting for project save` | Enabled, but the project has never been saved to disk. Save it and registration proceeds |
| `No Convoy host app` | No host app on this machine — or one that vanished mid-call. **Normal, and not an error.** Nothing is wrong; there is simply nothing to talk to |
| `Host app stale` | A portfile exists but nothing usable answers on it: the writing process is gone, or something else now holds that port. Same action as absence — nothing is sent |
| `Host app found` | A live, identity-confirmed host app was located. A registration result normally replaces this within the same call |
| `Registering...` | A registration call is in flight. Not logged — it passes through on every heartbeat |
| `Registered -- Envoy port pending` | The node is registered, but Envoy has not bound a port yet, so the host app cannot dispatch back. Temporary: Envoy binds seconds after open, and the tick keeps converging until it does |
| `Registered <node> (host <host>)` | Steady state. Both ids are shown as their first 8 characters |
| `Refused: <reason>` | The host app refused on policy — a decision it made, not a fault. Common reasons: `unknown_node`, `node_identity_conflict`. Not retried hard |
| `Error: <reason>` | The host app answered, but failed (a 5xx, or a request that never left this process). Retried with a jittered 5 s → 60 s backoff |
| `Error: <detail>` | A client-side or transport fault, with the detail attached |
| `Error: the register call timed out` | The worker never published a result inside its budget. The same string appears for `unregister` |
| `Error: no convoy id -- turn Convoy Enable off and on again to mint one` | Convoy is on but `.embody/project.json` has no convoy key — the key was removed, or a persisted toggle was restored onto a `project.json` that lost it. No network work is attempted |
| `Error: convoy_client module missing` | The `convoy_client` module is not in the `convoy` COMP. Reinstall Embody |
| `Error: unreadable result` | A result arrived that the status mapper could not read |
| `Error: no result` | A call produced no result object at all |
| `Error: unexpected state '<state>'` | A state string the mapper does not know. This one is a bug — please report it |

During **Perform Mode** the readout is left exactly as the show found it and no network work happens at all. Reconciliation resumes when Perform Mode ends.

## Parameters

Three parameters on the Embody COMP's **Convoy** page, documented in full in the [Parameter Reference](../embody/parameters.md#convoy):

| Parameter | Type | What it does |
|---|---|---|
| [`Convoyenable`](../embody/parameters.md#par-convoyenable) | Toggle | The one gate. Off is the default and does nothing at all |
| [`Convoyid`](../embody/parameters.md#par-convoyid) | Str (read-only) | Projects the id from `.embody/project.json`. Empty until the first explicit enable, and deliberately not baked into the tracked `Embody.tdn` or any released `.tox` |
| [`Convoystatus`](../embody/parameters.md#par-convoystatus) | Str (read-only) | The live readout above |

## Where to go next

- [The Convoy host app](host-app.md) — what it is, what it does, and what is and is not available today
- [Envoy security model](../envoy/security.md) — the boundary Convoy relays into
- [Changelog](../changelog.md) — Convoy's shipping history
