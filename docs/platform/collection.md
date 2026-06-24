# The Collection

The [Collection](https://embody.tools/collection) is a gallery of **Specimens** — transparent TouchDesigner networks shared as [TDN](../tdn/index.md). Every card is a real network you can read, inspect, and reuse; nothing is an opaque binary.

## Browse & filter

- Filter by **category** (generative, compositing, 3D, simulation, raymarching/SDF, audio-reactive, shaders, and more), **difficulty**, and hardware **requirements** — or full-text search.
- Each card flips between a **rendered result** and the **live node graph** (an in-browser TDN viewer), so you can see both what it makes and how it's wired.

## Inspect a Specimen

Open a Specimen to see its full node graph, metadata, the capability **scan verdict**, and its TDN. Because a Specimen *is* its network as text, you're reading exactly what you'd run.

## "Embody it" — copy into your project

The **embody it** button copies the Specimen's TDN to your clipboard. In a TouchDesigner project running [Embody](../embody/index.md), the clipboard watcher detects it and offers to paste it as a new COMP — see [Clipboard Auto-Paste](../embody/configuration.md).

!!! warning "Community networks paste inert"
    A Specimen copied from the Collection is imported **inert by default**: Execute DATs are disarmed, expressions neutralized, IO bypassed, and storage stripped — the structure and content are preserved, but nothing runs until you deliberately enable it. This is the safe default for running someone else's network.
