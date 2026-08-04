# Convoy

**Convoy** connects Embody-enabled TouchDesigner projects on the same trusted LAN. Each open project is a **node**: it can be selected as a remote target, and it can originate work for another node. There is no permanent controller, primary machine, or network leader.

The everyday path is:

```text
VS Code / Cursor / another AI client
        -> Envoy in the local Embody
        -> Convoy
        -> a selected remote Embody node
        -> TouchDesigner or an approved computer action
```

Several Embody nodes can run on one computer. They share an IP address but remain separately addressable.

!!! warning "Development preview"
    Convoy is under active development. The LAN runtime and user controls are being built and tested, but production host-app packaging is not complete in every build. Windows-to-Windows acceptance testing is in progress. On macOS the enable, install, and daemon-runtime path has been validated on Apple Silicon hardware, but LAN node-to-node operation on macOS has not yet been physically validated. Do not treat this preview as a production certification.

## How membership works

**Enable Convoy** is the membership and exposure gate. Turning it on says, “this node may join the Convoy on this trusted LAN.” There are no Create, Join, invitation, role, or “Expose This Node” controls.

- If a compatible Convoy is already present, the node joins it automatically.
- If none is present, enabled nodes establish one automatically.
- Nodes discover changes and reconnect without a manual refresh.
- Membership is remembered locally across ordinary restarts. Network-visible node status converges automatically, while dangerous permissions and resource limits remain local settings that peers cannot overwrite.
- Every node is a sibling. A host going offline makes its own nodes unavailable, but reachable siblings continue communicating.
- Turning **Enable Convoy** off prevents that node from both sending and receiving Convoy work and withdraws it from the LAN.

The first time you enable Convoy on a machine, Embody asks once for confirmation, naming the trusted-LAN scope it grants and the background host app it will install. That consent is remembered per install: later projects mint their identity silently, and the Setup Wizard's Convoy step counts as the same answer, so you are never asked twice. The node's convoy identity and the granted scope are recorded in the committed `.embody/project.json`, which project clones inherit. A project that has never been saved cannot enable Convoy -- Embody explains why and turns the toggle back off until you save.

Convoy does not require an attached AI client. When **AI Client** is **None**, enabling Convoy keeps Envoy's loopback command server running only as Convoy's internal TouchDesigner relay; it does not generate client configuration or launch a coding tool. Turning Convoy off stops that otherwise-unused internal server.

Convoy is intentionally convenient on a trusted production network. That also means it must not be enabled on guest Wi-Fi, public networks, or a LAN shared with people or devices you do not trust. Ordinary relayed tools can inspect and change a TouchDesigner project; the additional TD Python and Full Shell gates protect still more powerful interfaces.

## Set up a Convoy

Repeat these steps on every participating computer:

1. Save the TouchDesigner project. A saved `.toe` gives the node a stable project identity and a useful automatic name.
2. Choose an AI assistant if you want one. Convoy also works with **None**: Embody keeps only its internal local command service enabled and does not configure or launch an AI client.
3. In the [Setup Wizard](../embody/setup-wizard.md), choose **Enable Convoy**. You can also turn on **Enable Convoy** later from the Embody COMP's **Convoy** page.
4. Approve the one-time confirmation. Enabling Convoy installs and starts the background host app automatically -- the confirmation (or the wizard's Convoy step) is the consent for the app and its login persistence. Use **Repair Host App** on the Convoy page only to repair a broken install or apply an update, and **Start Host App** after a deliberate stop.
5. Allow the Convoy host app through the operating-system firewall on the **private/trusted network profile only**.
6. Check **Status** and the **Convoy Nodes** sequence. Other enabled nodes should appear automatically.

Keep every node on the same Embody release for supported deployments. Version-skewed nodes may remain visible, but operations that cannot be proven compatible are limited or refused.

See [Setup and Troubleshooting](host-app.md) for a machine-by-machine checklist and status help.

## Node names and status

**Node Name** defaults to the computer hostname plus the saved `.toe` filename, for example:

```text
render-01 / lobby
```

You can type a clearer display name. The name is a label only; changing it does not change the node's identity or permissions. Duplicate names are allowed, so always confirm the IP and other status details before sending consequential work.

The read-only **Convoy Nodes** sequence shows one row per known node:

| Column | Meaning |
|---|---|
| **Node Name** | Automatic or user-supplied display name |
| **IP Address** | Current address; several nodes can correctly show the same IP |
| **Status** | Online, offline, limited, incompatible, conflict, or an actionable error. An incompatible node's Embody version is folded into this column when it is the point |
| **Last Seen** | Age of the most recent presence update when the host reports it; otherwise the current observed online/offline state |

Per-node controller counts and richer detail are deliberately not standing columns; use `convoy_list_controllers` and `convoy_list_nodes` for live sessions and full node records.

Offline rows may remain visible so a temporarily closed or disconnected project does not disappear from the operator's mental map -- an offline node is still remotely launchable. Convoy routes with stable identities, not names or IP addresses. Genuinely dead rows clear themselves: the host app's retention sweep forgets a node whose project file has been deleted (once it has been silent for half an hour), and any node unseen for 30 days, always sparing nodes with unresolved work. An unplugged or unmounted drive never counts as deleted. To remove a specific row immediately, use the `convoy_forget_node` tool.

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
9. `convoy_list_controllers` shows live client sessions, selected targets, leases, and active work without waking TouchDesigner.
10. `convoy_start_node` can reopen a previously registered, currently offline node; `convoy_restart_node` safely replaces one exact running TouchDesigner process.

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

request = op.Embody.op('convoy').ext.ConvoyExt.listNodes(callback=on_nodes)
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

Remote Wake means “temporarily leave Perform Mode.” It does not launch a closed TouchDesigner process. Use the separate lifecycle tools described below when they are present in your preview build.

## Start or restart a TouchDesigner node

`convoy_start_node` asks the target computer's host app to reopen a known offline node. The node must have registered successfully before, and its saved `.toe` file and TouchDesigner application must still match that local launch record. Starting an already-running node is harmless and reports that it is already online.

`convoy_restart_node` addresses one exact running node and requires the runtime ID returned by `convoy_list_nodes`. This comparison prevents a delayed request from restarting a newer TouchDesigner process that happens to have taken the old node's place. Give each intended action a unique `idempotency_key`; if a response is lost, retry with the same key so Convoy can reconcile the original attempt instead of launching twice.

The normal restart policies are:

| Policy | Behavior |
|---|---|
| `require_clean` | Default. Refuses to restart if the project is dirty, unsaved, or its state cannot be verified. |
| `save_then_restart` | Saves first, verifies the saved state, and then restarts. A failed or uncertain save stops the restart. |

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

Structured Git and GitHub actions use named, bounded operations rather than accepting an arbitrary command line. The initial catalog focuses on repository status, remotes, branches/revision, safe fetch/pull/push, and read-only GitHub inspection; destructive Git history rewrites and force operations are not part of the default surface.

**Allow Execute TD Python** is effectively code execution as the user running TouchDesigner. TD Python can access files, the network, credentials available to TD, and process APIs. It is a separate gate from **Allow Full Shell**; leaving Full Shell off does not sandbox Python.

**Allow Full Shell** permits arbitrary operating-system commands as the logged-in user. It applies to the computer's Convoy host, including its local nodes, and can only be enabled locally. Leave it off on show machines unless a specific workflow requires it. Convoy peers cannot remotely turn on either dangerous permission.

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

## Version and platform compatibility

Use the same Embody version on all nodes. That is the supported and easiest-to-debug configuration.

Convoy checks capabilities per operation, so a minor mismatch does not have to hide a node completely. Presence and some read-only actions may continue while unsupported mutations are marked **Limited** or refused. A fundamentally incompatible version or safety contract is not bypassed for convenience.

The compatibility target is:

- Windows x64 to Windows x64
- Windows x64 to Apple Silicon macOS, in both directions
- Apple Silicon macOS to Apple Silicon macOS

TouchDesigner also runs on Intel macOS, but Convoy's first macOS target is Apple Silicon. The current preview has not completed physical macOS validation. Check the release notes before using Convoy in a show-critical deployment.

## Owlette

Owlette is optional. Convoy on a LAN does not require an Owlette account, internet access, or an Owlette agent.

The first Owlette bridge is intentionally small. `convoy_owlette` consumes only Owlette's supported public API for site and machine inventory, online detail, and command status. It is optional and fails closed when credentials or a published API primitive are unavailable.

For the current preview, provide `OWLETTE_API_KEY` in the Convoy host app's process environment (or `OWLETTE_API_KEY_SECRET` to reference a key held in the operating system's secret store), then restart the host app. `OWLETTE_SITE_ID` can provide an optional default site. Ask `convoy_owlette` for `capabilities` before sending another action; inventory and status calls do not wake TouchDesigner.

Owlette command submission has an additional local host opt-in: set `EMBODY_CONVOY_ALLOW_OWLETTE_COMMANDS=1` in that same host environment and restart the host app. Every submission also requires a caller-held idempotency key. Leave this setting absent or false on machines where the bridge should remain read-only. The generic `mcp_tool_call` command is refused, so Owlette cannot become an unrestricted shell or an ad hoc Convoy tunnel. Convoy and Owlette keep distinct identities, and no Owlette repository, agent, protocol, or API change is required.

## Next steps

- [Setup and Troubleshooting](host-app.md)
- [Setup Wizard](../embody/setup-wizard.md)
- [Convoy Parameter Reference](../embody/parameters.md#convoy)
- [Envoy Tools Reference](../envoy/tools-reference.md)
