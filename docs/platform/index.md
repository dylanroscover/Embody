# embody.tools — the Collection

[**embody.tools**](https://embody.tools) is the public home for **Specimens**: transparent TouchDesigner networks you can inspect, copy, and drop straight into your own project. Where a typical "download this `.tox`" is an opaque binary, every Specimen is a **TDN** network — human-readable YAML you can read before you run, and paste into any TouchDesigner `.toe` running [Embody](https://github.com/dylanroscover/Embody).

The site is built on Astro + Cloudflare Workers and is **fully browsable without an account**.

## What's here

- **[The Collection](collection.md)** — browse Specimens, inspect their node graph in the browser, and copy a Specimen's TDN to your clipboard ("embody it").
- **[Contributing a Specimen](contribute.md)** — share your own network. Every contribution is capability-scanned before it goes live.
- **[Accounts](accounts.md)** — a free account (email + password, or GitHub) lets you submit and react. Browsing needs no account.

## How "embody it" works

1. On a Specimen, hit **embody it** — its TDN is copied to your clipboard.
2. In a TouchDesigner project running Embody, the **clipboard watcher** sees it and prompts to drop it in as a new COMP — no keyboard shortcut needed. See [Clipboard Auto-Paste](../embody/keyboard-shortcuts.md).
3. Community Specimens are imported **inert by default** — Execute DATs disarmed, expressions neutralized, IO bypassed — so a pasted network can't run anything until you choose to enable it.
