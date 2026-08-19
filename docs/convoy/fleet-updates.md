# Fleet Updates

Update Embody on Convoy nodes without touching each machine -- and without
granting **Allow Execute TD Python** anywhere. `update_embody` is a bounded,
registered Convoy operation: the node's own updater fetches the official
GitHub release, verifies the sha256-pinned manifest, refuses downgrades and
TD-build-floor violations, and swaps the component in place. No caller code
or URL crosses the wire.

## The two-call flow

```
convoy_list_nodes                       -> who is below latest
convoy_update_embody(node="render-3")   -> durable update job on that node
```

or the whole fleet at once:

```
convoy_update_embody(all=true)
```

Each dispatch returns a per-node `delivery_id`; poll with
`convoy_get_job(delivery_id=...)`. A finished job carries
`version_before` / `version_after`; a node already at the latest release
finishes `done` with the versions equal. A successful install restarts the
node's MCP server, so its next call may ride one reconnect blip.

Nodes are skipped (and named in `skipped`) when they are offline, disabled,
or in Perform Mode -- never update a machine mid-show.

## Requirements and caveats

- **The target must run Embody >= 6.0.256.** Older nodes do not serve the
  `update_embody` operation and refuse it as unknown -- they need one manual
  update first (or the update via TD Python where that is already granted).
- **The node needs GitHub reachability.** The updater fetches
  `releases/latest` and the release assets itself; an offline show LAN
  reports a network error in the job record instead of updating.
- **`update_embody` never needs the TD Python grant.** It is classified
  `executes_arbitrary_code=False` in the Convoy operation registry, and it
  is deliberately the remote-exposed one of the fleet verbs -- `run_tests`
  counts as arbitrary code, and `save_project` is local-only.
- **Persistence**: the updater installs into the live component. If your
  project embeds Embody inside a `.tox` you manage yourself, re-save that
  component (or the project) after the update, or the machine reverts on
  its next open. Saving is a local step -- `save_project` is deliberately
  not remote-exposed -- so save from a session on that machine, or use
  `convoy_restart_node` with its `save_then_restart` policy.

## Traps (learned in the field)

- The Embody **Update** pulse is *not* the self-updater -- it re-exports
  externalizations. The self-update entry is `Checkforupdate` /
  `UpdaterExt.CheckForUpdate`.
- A **dev checkout** refuses the update (the updater protects a source
  checkout); the refusal lands in the job record instead of a swap.
- A second `update_embody` while one is **already in flight** reconciles
  to the existing job handle -- poll that job, don't expect a new one.
- A **disabled local node** never appears in `convoy_list_nodes` (the host
  filters it before the bridge sees it), so it reads as missing from the
  list, not as `skipped`.
- `CheckForUpdate(interactive=True)` (the parameter pulse's default) raises
  a blocking modal -- on an unattended machine use the `update_embody`
  operation, which always runs `interactive=False, auto_install=True`.
- Version metadata is already in `convoy_list_nodes` (`embody_version`
  per node) -- never `get_op` the Embody COMP for it (a ~230-parameter
  read). For anything else, `get_parameter` with a search pattern.
- Local nodes in `convoy_list_nodes` carry a `capabilities` field
  (`td_python`, `full_shell`). Remote nodes deliberately do not --
  capability grants are never advertised across the LAN -- so absence
  means *unknown*, never *allowed*.
