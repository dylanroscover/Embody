# The Collection

The [Collection](https://embody.tools/collection) is a gallery of **Specimens** — transparent TouchDesigner networks shared as [TDN](../tdn/index.md). Every card is a real network you can read, inspect, and reuse; nothing is an opaque binary.

## Browse & filter

- Filter by **category** (generative, compositing, 3D, simulation, raymarching/SDF, audio-reactive, shaders, and more), **level**, and hardware **requirements** — or full-text search.
- Each card flips between a **rendered result** and the **live node graph** (an in-browser TDN viewer), so you can see both what it makes and how it's wired.

## Inspect a Specimen

Open a Specimen to see its full node graph, metadata, the capability **scan verdict**, and its TDN. Because a Specimen *is* its network as text, you're reading exactly what you'd run.

## "Embody it" — copy into your project

The **embody it** button copies the Specimen's TDN to your clipboard. In a TouchDesigner project running [Embody](../embody/index.md), the clipboard watcher detects it and offers to paste it as a new COMP — see [Clipboard Auto-Paste](../embody/configuration.md). How it pastes (live, disarmed, or rejected) depends on the capability-scan verdict described below.

!!! info "How community networks are imported"
    A Specimen copied from the Collection is run through a capability scanner on import, and the verdict decides how it pastes:

    - **Clean** — pastes in live and fully working, with no neutralization and no warning. This is the common case.
    - **Flagged** (notable capability surfaces) — pastes disarmed: Execute/Script DATs are bypassed, dangerous expressions are neutralized, `tox_ref`/`tdn_ref` shells and global op shortcuts are stripped, IO is bypassed, and storage is stripped. Provably *pure* value expressions — parameter reads, `absTime`, `math.*`, `Par.eval()`, arithmetic, ternaries — are **preserved**, so the network keeps its non-dangerous logic and still renders. Trusted TouchDesigner palette extensions (`op.TD<Name>`) are kept.
    - **Blocked** — rejected on the server and never imported.

    A TDN you copied from your own project (not from the Collection) always pastes live and trusted — it is not scanned.
