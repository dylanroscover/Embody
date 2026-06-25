# embody.tools — the Collection

[**embody.tools**](https://embody.tools) is the public home for **Specimens**: transparent TouchDesigner networks you can inspect, copy, and drop straight into your own project. Where a typical "download this `.tox`" is an opaque binary, every Specimen is a **TDN** network — human-readable YAML you can read before you run, and paste into any TouchDesigner `.toe` running [Embody](https://github.com/dylanroscover/Embody).

The site is built on Astro + Cloudflare Workers and is **fully browsable without an account**.

## What's here

- **[The Collection](collection.md)** — browse Specimens, inspect their node graph in the browser, and copy a Specimen's TDN to your clipboard ("embody it").
- **[Contributing a Specimen](contribute.md)** — share your own network. Every contribution is capability-scanned before it goes live.
- **[Accounts](accounts.md)** — a free account (email + password, or GitHub) lets you submit and react. Browsing needs no account.

## How "embody it" works

1. On a Specimen, hit **embody it** — its TDN is copied to your clipboard.
2. In a TouchDesigner project running Embody, the **clipboard watcher** sees it and prompts to drop it in as a new COMP — no keyboard shortcut needed. See [Clipboard Auto-Paste](../embody/configuration.md).
3. Community Specimens (from embody.tools) are **capability-scanned on import**. A **clean** Specimen — the common case — pastes in live and fully working, with no neutralization. A **flagged** Specimen pastes disarmed: side-effecting surfaces are disabled (Execute/Script DATs bypassed, dangerous expressions neutralized, IO bypassed, storage stripped, external `tox_ref`/`tdn_ref` shells removed) — but provably pure value expressions (parameter reads, `absTime`, math, arithmetic) are **kept**, so the network still renders. A **blocked** Specimen is rejected on the server and never reaches you. A Specimen you copied from your own project always pastes live and trusted.
