# Convoy

**Convoy** connects Embody-enabled TouchDesigner projects on the same trusted LAN. Each open project is a **node**: it can be selected as a remote target, and it can originate work for another node. There is no permanent controller, primary machine, or network leader.

The everyday path, drawn with the machine boundary where it actually is:

```text
 YOUR COMPUTER                             REMOTE COMPUTER
+-----------------------------+           +-------------------------------+
|  VS Code / Cursor /         |           |  TouchDesigner                |
|  another AI client          |           |    the selected Embody node   |
|      |                      |           |    runs the operation         |
|      v  MCP (loopback)      |           |        ^                      |
|  Envoy in the local Embody  |           |        |  loopback            |
|      |                      |           |        |                      |
|      v  loopback            |           |        |                      |
|  Convoy host app  ==========+===========+==> Convoy host app            |
|  (per-user background app)  |  trusted  |   (one per computer, serving  |
+-----------------------------+  LAN,     |   every node on it; approved  |
                                 mutual   |   computer actions run here   |
                                 TLS      |   even with TouchDesigner     |
                                          |   closed)                     |
                                          +-------------------------------+
```

Only one hop crosses the network: host app to host app, over the mutually authenticated LAN transport. Everything else -- your AI client to Envoy, Envoy to its host app, the remote host app to its nodes -- stays loopback on its own computer. That is also the security shape: Envoy itself is never exposed to the LAN, on either end.

Several Embody nodes can run on one computer. They share that computer's single host app and IP address but remain separately addressable.

!!! warning "Development preview"
    Convoy is under active development. The LAN runtime and user controls are being built and tested, but production host-app packaging is not complete in every build. Windows-to-Windows acceptance testing is in progress. On macOS the enable, install, and daemon-runtime path has been validated on Apple Silicon hardware, but LAN node-to-node operation on macOS has not yet been physically validated. Do not treat this preview as a production certification.

## How membership works

Convoy has two levels, and they keep separate hours:

- A **machine** is a computer running the Convoy host app. TouchDesigner is how it gets set up, once. From then on the machine stays in the Convoy at every login, reachable from the others, whether or not any project is open.
- A **node** is one project on a machine. Every save of a project is the same node: `e1.toe`, `e1.5.toe` and `e1.6.toe` share one identity, one address and one set of approvals, so a version-up changes nothing but the file name. `show.toe` and `rig.toe` are two nodes, and so is one file name in two folders.

**Enable Convoy** is the gate for one project. There are no everyday Create, Join, invitation, role, or “Expose This Node” controls -- the one explicit Join lives inside the **Resolve Realm Conflict...** recovery dialog described in [Setup and Troubleshooting](host-app.md#recovering-from-a-realm-conflict).

- If a compatible Convoy is already present, the node joins it automatically.
- If none is present, enabled nodes establish one automatically, after a short randomized listen window of 8-20 seconds. Operations attempted during it report `realm_not_established` while genesis settles. The randomization is deliberate: enable two machines at the same moment and they converge on one Convoy instead of each founding its own.
- Nodes discover changes and reconnect without a manual refresh.
- Membership is remembered locally across ordinary restarts. Network-visible node status converges automatically, while dangerous permissions and resource limits remain local settings that peers cannot overwrite.
- Every node is a sibling. A host going offline makes its own nodes unavailable, but reachable siblings continue communicating.
- Once a machine has accepted Convoy, every saved project opened there joins by itself. Saying no sticks: turn **Enable Convoy** off for that project, or answer no in the Setup Wizard, and it stays out. Opening a project never restarts a host app you stopped.
- Turning **Enable Convoy** off stops that project sending and receiving Convoy work. The machine stays in the Convoy. To take the whole computer off the network, press **Stop Convoy App**; it stays out across logins and project opens until you press **Start Convoy App**.
- Convoy remembers every project that has opened on a machine, with its approvals and launch record, until its `.toe` is deleted or you forget it. A closed project is simply not listed.

The first time you enable Convoy on a machine, Embody asks once for confirmation, naming the trusted-LAN scope it grants and the background host app it will install. That consent is remembered per install: later projects mint their identity silently, and the Setup Wizard's Convoy step counts as the same answer, so you are never asked twice. The node's convoy identity and the granted scope are recorded in the committed `.embody/project.json`, which project clones inherit. A project that has never been saved cannot enable Convoy -- Embody explains why and turns the toggle back off until you save.

Some fleets deliberately do **not** track `.embody/project.json` -- the same repo is mirrored onto every machine, and each box keeps its own identity. That works: if your `.gitignore` ignores the file, Envoy leaves it ignored (see [Auto-managed .gitignore](../embody/getting-started.md#auto-managed-gitignore)). A fresh machine then either gets `.embody/project.json` pre-seeded during provisioning, or simply enables Convoy and adopts the established realm during the listen window above -- a new identity only founds its own Convoy when no established one is heard on the LAN.

A project whose committed binding names a *different* established Convoy -- a clone of a repo from another studio's LAN, for example -- is refused rather than merged, and its **Status** reads `Refused: local_realm_conflict`. Toggling **Enable Convoy** on arms a one-shot offer for about two minutes: when the refusal arrives inside that window, Embody opens **Rejoin Local Convoy**, naming both realms. **Keep Binding** leaves the project bound as it is and refused on this LAN; **Rejoin Local Mesh** rebinds the git-tracked `.embody/project.json` to this machine's mesh, which it adopts on the next registration. That rebind travels to every clone of the repo, so clones still on the original mesh will be refused there until they rejoin the same way. If the *machine* itself is in conflict, resolve that first -- see [Recovering from a realm conflict](host-app.md#recovering-from-a-realm-conflict).

Convoy does not require an attached AI client. When **Configure For** is set to **None**, enabling Convoy keeps Envoy's loopback command server running only as Convoy's internal TouchDesigner relay; it does not generate client configuration or launch a coding tool. (The test is **Configure For**, not **Launch Client** -- those are separate settings, and leaving Launch Client set while Configure For is None is fine.) Turning Convoy off stops that otherwise-unused internal server.

Convoy is intentionally convenient on a trusted production network. That also means it must not be enabled on guest Wi-Fi, public networks, or a LAN shared with people or devices you do not trust. Ordinary relayed tools can inspect and change a TouchDesigner project; the additional TD Python and Full Shell gates protect still more powerful interfaces.

## Set up a Convoy

Do this once on every participating computer. Other projects on that computer then join by themselves when they open.

1. Save the TouchDesigner project. A saved `.toe` gives the node a stable project identity and a useful automatic name.
2. Choose an AI assistant if you want one. Convoy also works with **None**: Embody keeps only its internal local command service enabled and does not configure or launch an AI client. Either way, Convoy runs on Envoy: the host app runs in the Python environment Envoy builds (`.venv`), and remote work reaches TouchDesigner through Envoy's local command service. **Enable Convoy** therefore turns **Enable Envoy** on if it is off, and **Status** reads `Needs Envoy` whenever Envoy is off.
3. In the [Setup Wizard](../embody/setup-wizard.md), choose **Enable Convoy**. You can also turn on **Enable Convoy** later from the Embody COMP's **Convoy** page.
4. Approve the one-time confirmation. Enabling Convoy installs and starts the background host app automatically -- the confirmation (or the wizard's Convoy step) is the consent for the app and its login persistence. Use **Repair Convoy App** on the Convoy page only to repair a broken install or apply an update, and **Start Convoy App** after a deliberate stop.
5. Allow the Convoy host app through the operating-system firewall on the **private/trusted network profile only**.
6. Check **Status** and the **Convoy Machines** sequence. Other machines should appear automatically, whether or not they have a project open.

Keep every node on the same Embody release for supported deployments. Version-skewed nodes may remain visible, but operations that cannot be proven compatible are limited or refused.

See [Setup and Troubleshooting](host-app.md) for a machine-by-machine checklist and status help.

## Node names and status

**Node Name** defaults to the computer hostname plus the saved `.toe` filename, for example:

```text
render-01 / lobby
```

You can type a clearer display name. The name is a label only; changing it does not change the node's identity or permissions. Duplicate names are allowed, so always confirm the IP and other status details before sending consequential work.

The automatic name is stored in the project file, so a `.toe` deployed to another computer arrives wearing the machine name it was saved on. That stamp heals itself: on the receiving machine the name refreshes to the local hostname and project the next time the node initializes, and until it does, the node list prefixes the row with the machine's live hostname -- for example `render-02 (render-01 / lobby)` -- so cloned projects always stay tellable-apart. A custom display name that does not look like an automatic stamp is never rewritten.

The read-only **Convoy Machines** sequence shows one row per computer, so an address is never listed twice:

| Column | Meaning |
|---|---|
| **Machine** | The computer's name and current address, for example `TEC-B4A / 192.168.88.20` |
| **Open Projects** | The `.toe` files active on it right now, for example `Project.1.toe, Moonshine.toe, e1.toe`. `None open` is normal: the machine is still in the Convoy |
| **Status** | Reachability and age together, for example `Online / 5s ago` or `Offline / 2h ago -- peer unreachable`. A machine whose host app cannot prove every operation adds it, for example `Online / 5s ago -- limited: 12 of 19 operations shared with host app 6.0.280`. `convoy_list_nodes` carries the same as `compatibility`, `compatibility_reason` and `host_app_version` |

Per-project detail lives in `convoy_list_nodes`: its `machines` list mirrors this table, and its `nodes` list has every project the machines know about, including closed ones, with `offline_reason`, approvals and versions. Convoy routes with stable identities, not names or IP addresses.

A closed project stays known so it can still be started remotely and keeps its TD Python approval. It clears itself about half an hour after its project file is deleted (an unplugged or unmounted drive never counts as deleted, and a node with a delivery that has not finished is always spared). To remove something sooner, press **Forget Offline...** on the Convoy page: it names the closed projects and offline machines it would remove before anything is forgotten, or use the `convoy_forget_node` tool for a single known id. **A project is forgotten by the computer that owns it, from wherever you press**: other machines' rows are relayed to them over their live session, and only a machine that is not connected right now is left for later and named. Forgetting a machine removes this computer's record of it and its trust in that identity until it rejoins.

## Use Convoy from an AI client

The MCP workflow is explicit about where work runs:

1. `get_convoy_status` checks that the local Convoy host app is available.
2. `convoy_list_nodes` lists local and reachable remote nodes; `convoy_ping` checks one node's liveness through its host app without waking TouchDesigner.
3. `convoy_select_node` pins this client session to one exact node. Ordinary Envoy tools then run there until you call `convoy_select_node` with `clear=true`.
4. `convoy_call` sends a one-off registered operation without changing the session selection.
5. `convoy_batch` runs the same ordered batch on one or more explicit targets and reports each target separately. It does not promise multi-machine rollback.
6. `convoy_get_job` checks durable work that outlives the original tool call or reconnect; `convoy_ack_job` acknowledges a finished delivery you have safely observed, letting the target release its protected result artifacts (verified artifact downloads and saves acknowledge automatically); `convoy_cancel_job` requests cancellation from the exact owning host.
7. `convoy_get_artifact` retrieves a large JSON, text, or file result from its artifact reference and verifies it into a temporary local file. Relayed screenshots are normally retrieved automatically.
8. `convoy_save_artifact` verifies an artifact and saves it into the current client's Embody project. An optional plain filename can be supplied; existing files are preserved unless `overwrite=true` is explicit.
9. `convoy_send_file` sends one file from the current project to a node (see [Sending files to a node](#sending-files-to-a-node)).
10. `convoy_list_controllers` shows live client sessions, selected targets, leases, and active work without waking TouchDesigner.
11. `convoy_start_node` can reopen a previously registered, currently offline node; `convoy_restart_node` safely replaces one exact running TouchDesigner process.

### Choosing the channel

- **Reach a remote node through Convoy, not SSH.** Convoy carries the node's own permission gates, writer lease and audit trail; SSH carries none of them. Fall back to SSH only when Convoy cannot reach the node and `convoy_start_node` has failed.
- **Code and project content move by git:** commit and push from one machine, then fetch and pull (or switch branches) on the other through the structured Git actions below. A pull into a node's live project changes files under its open TouchDesigner; use a worktree when the live project must not change during a show.
- **A single file that is not in git** goes with `convoy_send_file`; **results come back** with `convoy_get_artifact` / `convoy_save_artifact`.

You can usually ask in plain language. For example:

> List my Convoy nodes, then query `/project1` on `render-02 / lobby` and report any operator errors. Do not modify anything.

For a mutation, name the target and scope clearly:

> On `render-03 / facade`, change only `/project1/output/level1`, then verify that operator for errors.

Convoy never silently falls back to a local TouchDesigner instance when an explicit remote target cannot be reached.

## Use Convoy from TouchDesigner Python

An enabled node can also originate work without an attached AI client. The API is asynchronous: it returns a local request handle immediately, and an optional callback receives progress and completion events on TouchDesigner's main thread.

Convoy's extension lives on Embody's `convoy` child COMP, so TouchDesigner Python reaches it through `op.Embody.op('convoy').ext.ConvoyExt`.

```python
def on_nodes(event):
    if event.get('event') == 'complete':
        debug(event.get('result'))

request = op.Convoy.ext.ConvoyExt.listNodes(callback=on_nodes)
```

Use the `host_id` and `node_id` returned by that list for an exact sibling call:

```python
def on_result(event):
    if event.get('event') == 'complete':
        debug(event.get('result'))

request = op.Embody.op('convoy').ext.ConvoyExt.call(
    host_id='host-id-from-list',
    node_id='node-id-from-list',
    operation='query_network',
    arguments={'root': '/'},
    wait=False,
    callback=on_result,
)
```

Keep the returned handle if you need to inspect it later with `requestResult()`. `ping()`, `batch()`, `getJob()`, and `cancelJob()` follow the same callback pattern. Disabling Convoy prevents this node from both originating and receiving sibling work.

## Controllers, edit leases, and jobs

A **controller** is an active client session, such as one Cursor window or one Envoy session. It is not a membership role and does not outrank another controller. Controller counts are visible in the status sequence; the detailed controller list is intended to show who is active, which node they selected, and whether work is in progress without waking TouchDesigner.

Convoy supports concurrent readers and tracks bounded **edit leases** for coordinated work:

- Read and status work can proceed together.
- A mutation is refused while another live controller holds an incompatible exclusive node lease.
- Ordered work sent with `convoy_batch` is one target-side Envoy batch. Fanout targets remain independent and may partially succeed.
- A conflict is reported with the current owner/context; Convoy does not guess, steal control, or imply distributed rollback.
- Expired sessions and leases are cleaned up automatically.

This is the network counterpart to Envoy's local multi-session scoping, not a permanent lock the user must manage for ordinary calls.

Long-running and relayed operations are represented as **jobs**. Jobs survive a client disconnect and can be checked after the network returns. If a call times out, that does not prove it never ran: use the returned job or retry identity to reconcile before issuing a different mutation. Cancellation is best effort once work has started; always inspect the target before retrying an uncertain write.

## Perform Mode and Remote Wake

**Remote Wake** is on by default. When a permitted remote TouchDesigner operation arrives while the target is in Perform Mode, Convoy temporarily makes its command service available, completes the work, and restores the previous Perform state after the idle grace period.

- **Remote Wake Idle Grace** defaults to **60 seconds**.
- New remote work during that window restarts the timer.
- Discovery, status, ping, job polling, and controller queries do **not** wake TouchDesigner.
- Turning **Remote Wake** off leaves the node discoverable but refuses work that requires waking it.
- `save_project` and `update_embody` never run mid-show, and a node in Perform Mode is not woken for them. Its host app refuses the job (`refused`), or the node itself does (`failed`), including when only TouchDesigner's perform window is open; either way the job's result carries code `perform_mode` and nothing ran.

Remote Wake means “temporarily leave Perform Mode.” It does not launch a closed TouchDesigner process. Use the separate lifecycle tools described below when they are present in your preview build.

## Start or restart a TouchDesigner node

`convoy_start_node` asks the target computer's host app to reopen a known offline node. The node must have registered successfully before, and its saved `.toe` file and TouchDesigner application must still match that local launch record. Starting an already-running node is harmless and reports that it is already online.

`convoy_restart_node` addresses one exact running node and requires the runtime ID returned by `convoy_list_nodes`. This comparison prevents a delayed request from restarting a newer TouchDesigner process that happens to have taken the old node's place. Give each intended action a unique `idempotency_key`; if a response is lost, retry with the same key so Convoy can reconcile the original attempt instead of launching twice.

The normal restart policies are:

| Policy | Behavior |
|---|---|
| `require_clean` | Default. Refuses to restart if the project is dirty, unsaved, or its state cannot be verified. |
| `save_then_restart` | Saves first, verifies the saved state, and then restarts. A failed or uncertain save stops the restart; a dirty project on a node in Perform Mode is not saved, and the restart stops with `perform_mode`. |

The public Convoy relay accepts only these two restart policies. Discard/force behavior is not exposed as a remote recovery shortcut. A restart also refuses when several Embody nodes share the same TouchDesigner process, because restarting one would silently interrupt the others.

Cancellation and deadlines have an intentional safety boundary. Before the old process commits to exiting, either condition stops the restart without deliberately quitting that process. With `save_then_restart`, a response containing `save_may_have_run` means the save was handed to TouchDesigner but its final acknowledgement was uncertain; inspect the project file before retrying. After the quit commit, a response may report `cancel_deferred` or `deadline_deferred`: Convoy continues a bounded restoration attempt because abandoning the node while it is down would be less safe.

The host app records that commit durably. If it restarts during the replacement window, it automatically resumes reconciliation and updates the original job instead of treating the request as new work. Query the same job, or retry with the same idempotency key, until the exact node and runtime are confirmed online. Do not issue a second restart with a new key merely because the first client wait ended.

Remote lifecycle control is part of the development preview. Validate the complete quit, launch, registration, and crash-recovery path on your own machines before using it in a show-critical deployment.

## Permissions for code and computer actions

Convoy's ordinary registered TouchDesigner tools are available when the node is enabled and the request is compatible. More powerful code paths have separate local gates.

| Capability | Default | Approval |
|---|---|---|
| Registered TouchDesigner operations | Available | **Enable Convoy** on the target node |
| Structured Git and GitHub CLI actions | Available where supported | No Full Shell approval required |
| Arbitrary TouchDesigner Python | Off | Turn on **Allow Execute TD Python** locally on that node |
| Arbitrary operating-system shell | Off | Turn on **Allow Full Shell** locally on that computer |

Self-updating Embody on a node (`update_embody`, the operation behind `convoy_update_embody`) is a registered operation, not TD Python: it installs only the sha256-verified official release and refuses downgrades, so patching a fleet never requires the dangerous grant.

Saving a node's project (`save_project` through `convoy_call`) needs no grant either: it runs no caller code. **It is a development action, not for shows.** A save blocks that machine's TouchDesigner main thread for 15+ seconds, so no frames render, and restarts its Envoy server. Never send it to a machine running a show; a node in Perform Mode, or with TouchDesigner's perform window open, refuses it. `run_tests` is never relayed to another computer.

Structured Git and GitHub actions use named, bounded operations rather than accepting an arbitrary command line. The catalog covers repository status, remotes, branches/revision, fetch, fast-forward pull, push, branch switching, sibling worktrees, and read-only GitHub inspection; destructive Git history rewrites and force operations are not part of the default surface. Network Git is HTTPS-only.

- **Pull** fast-forwards only the branch that is already checked out; naming another branch is refused (`branch_mismatch`) instead of moving the checked-out one.
- **Switch branch** needs a clean tree. It switches to an existing local branch, or, when you name a `remote`, creates the branch to track `<remote>/<branch>`; it never guesses a remote.
- **Worktrees** are a choice, not a requirement: `worktree_add` creates a sibling checkout named `<project>-wt-<name>` beside the node's project, `worktree_list` lists them, and `worktree_remove` removes one only when it holds no local, untracked or ignored files, and never when it is itself a registered node's project. Every other Git action takes an optional `worktree` name and runs there instead of in the live project, so a running TouchDesigner never sees the change.

**Allow Execute TD Python** is effectively code execution as the user running TouchDesigner. TD Python can access files, the network, credentials available to TD, and process APIs. It is a separate gate from **Allow Full Shell**; leaving Full Shell off does not sandbox Python.

**Allow Full Shell** permits arbitrary operating-system commands as the logged-in user. It applies to the computer's Convoy host, including its local nodes, and can only be enabled locally. Leave it off on show machines unless a specific workflow requires it. Convoy peers cannot remotely turn on either dangerous permission.

Both toggles display the host app's current approval, not a stored project setting. Turning one on asks the local host app for the change: the toggle returns to its approved value and a confirmation dialog appears -- the grant takes effect (and the toggle shows On) once you confirm. If no dialog appears, the request did not reach the host app and nothing was granted. A value carried inside a saved `.toe`, a clone, or a script write is reverted automatically and never grants anything.

Git, GitHub CLI, and shell actions use the tools and credentials already configured for the logged-in user. Convoy does not create a second GitHub identity.

## Screenshots, files, and Artifact Quota

Screenshots, transferred files, large results, and command output are stored temporarily as **artifacts**. **Artifact Quota (MB)** limits the combined managed cache for the logged-in user on that computer across all local Convoy nodes. The default is **1024 MB**.

When a relayed screenshot returns a PNG or JPEG artifact, Convoy normally downloads and verifies it automatically and presents it to the AI client as image content. Once the image is inline, Convoy removes its bridge temporary file and does not report a retained temp path. The artifact reference remains available if you want to save it explicitly.

For a large JSON result, text result, or other file returned as an artifact reference, use `convoy_get_artifact`. Convoy retrieves the bytes from the owning host, verifies their size and SHA-256 digest, and returns a local temporary path. Pass the complete artifact reference and exact target information from the original result; do not substitute a displayed filename or a path reported by the remote computer.

Use `convoy_save_artifact` when the file must persist. It verifies the artifact again and saves it under the current Envoy client's own `.embody/convoy/artifacts/` directory; callers cannot redirect it into another project or arbitrary folder. You may provide one plain filename. The default `overwrite=false` protects an existing project file, so set `overwrite=true` only when replacement is intended.

When the quota is reached, Convoy removes the **least recently used** unpinned artifacts first. It does not remove active transfers, data required by running or not-yet-acknowledged jobs, temporary pins, or copies you explicitly saved into a project. If nothing safe can be removed, the new transfer is refused instead of deleting protected work. A quota of `0` disables operations that require managed artifact storage; it does not mean unlimited space.

The managed cache is outside the project:

| Platform | Managed cache |
|---|---|
| Windows | `%LOCALAPPDATA%\Embody\Convoy\cache\artifacts` |
| macOS | `~/Library/Caches/Embody/Convoy/artifacts` |

When you explicitly save or export an artifact into a project, it goes under:

```text
.embody/convoy/artifacts/
```

Those explicit copies are outside the runtime quota and are yours to retain, delete, ignore, or commit. Convoy never treats a path on another computer as though it were a local path; remote data must arrive as a verified artifact or a normal structured result. A remote `C:\...` or `/Users/...` path is descriptive target-side information only and must never be opened as a local result.

### Sending files to a node

`convoy_send_file` sends one file the other way. The source must resolve inside the sending client's own project, so a file elsewhere on that computer can never be sent. The local host app uploads it as a verified artifact to the host that owns the target node, then a durable `convoy_put_file` job writes it:

| Destination | How | Gate on the target |
|---|---|---|
| `.embody/convoy/inbox/<name>` in the node's project (default) | `dest` is a plain file name, defaulting to the source's | none beyond **Enable Convoy** |
| A path inside the node's `<project>-wt-<name>` worktree | `worktree=<name>`; `dest` defaults to the file's own project-relative path | none beyond **Enable Convoy** |
| A path inside the node's live project | `live=true` | **Allow Execute TD Python**, because a synced `.py` written there reloads into TouchDesigner |

Destinations are relative, `/`-separated paths; `..`, drives, links, junctions, short-name aliases, Windows-reserved or invalid names, anything under `.git`, AI client config (`.mcp.json`, `.claude/`, `.codex/`, `.cursor/`, `.vscode/`, `.gemini/`) and `.embody/` are refused. A worktree that is itself a registered node's project is refused too: target that node instead. An existing file is kept unless `overwrite=true`. Only the controller that uploaded a file can have it written.

## Version and platform compatibility

Use the same Embody version on all nodes. That is the supported and easiest-to-debug configuration.

Keeping a fleet current is one call: `convoy_update_embody(all=true)` dispatches each node's own bounded self-update (official release, sha256-verified, downgrade-refusing) as a durable per-node job -- no per-machine visits and no **Allow Execute TD Python** grant involved. See [Fleet Updates](fleet-updates.md).

Convoy checks capabilities per operation, so a minor mismatch does not have to hide a node completely. Presence and some read-only actions may continue while unsupported mutations are marked **Limited** or refused. A fundamentally incompatible version or safety contract is not bypassed for convenience.

The compatibility target is:

- Windows x64 to Windows x64
- Windows x64 to Apple Silicon macOS, in both directions
- Apple Silicon macOS to Apple Silicon macOS

TouchDesigner also runs on Intel macOS, but Convoy's first macOS target is Apple Silicon. The current preview has not completed physical macOS validation. Check the release notes before using Convoy in a show-critical deployment.

## Owlette

[Owlette](https://owlette.app) is optional. Convoy on a LAN does not require an Owlette account, internet access, or an Owlette agent.

The first Owlette bridge is intentionally small. `convoy_owlette` consumes only Owlette's supported public API for site and machine inventory, online detail, and command status. It is optional and fails closed when credentials or a published API primitive are unavailable.

For the current preview, provide `OWLETTE_API_KEY` in the Convoy host app's process environment (or `OWLETTE_API_KEY_SECRET` to reference a key held in the operating system's secret store), then restart the host app. `OWLETTE_SITE_ID` can provide an optional default site. Ask `convoy_owlette` for `capabilities` before sending another action; inventory and status calls do not wake TouchDesigner.

Owlette command submission has an additional local host opt-in: set `EMBODY_CONVOY_ALLOW_OWLETTE_COMMANDS=1` in that same host environment and restart the host app. Every submission also requires a caller-held idempotency key. Leave this setting absent or false on machines where the bridge should remain read-only. The generic `mcp_tool_call` command is refused, so Owlette cannot become an unrestricted shell or an ad hoc Convoy tunnel. Convoy and Owlette keep distinct identities, and no Owlette repository, agent, protocol, or API change is required.

## Next steps

- [Setup and Troubleshooting](host-app.md)
- [Fleet Updates](fleet-updates.md)
- [Setup Wizard](../embody/setup-wizard.md)
- [Convoy Parameter Reference](../embody/parameters.md#convoy)
- [Envoy Tools Reference](../envoy/tools-reference.md)
