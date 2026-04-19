---
hide:
  - navigation
---

# Embody

### Create at the speed of thought

Embody puts your ideas on screen as fast as you can describe them. Describe a network in plain language and watch it appear. Want to try a different direction? Spin up a new approach in seconds. Branch off the one that works. The tool keeps up with you, instead of the other way around.

Embody is three tools working together — *forward velocity*, *lateral velocity*, and the substrate that makes both possible.

<div class="grid cards" markdown>

-   :material-robot:{ .lg .middle } **Envoy** — Forward Velocity

    ---

    An embedded [MCP](https://modelcontextprotocol.io/) server with **46 tools** that lets Claude Code, Cursor, and Windsurf talk directly to your live TouchDesigner session. Say what you want — operators, connections, parameters, extensions, fixes — and watch it happen. Idea → network in seconds.

    [:octicons-arrow-right-24: Setup Envoy](envoy/setup.md)

-   :material-sync:{ .lg .middle } **Embody** — Lateral Velocity

    ---

    Tag any operator and Embody externalizes it to files on disk that mirror your network hierarchy. Try a new direction, branch off a good one, restore yesterday's state — all in seconds. Externalized files are the source of truth, so every project opens already in flow.

    [:octicons-arrow-right-24: Get started](embody/getting-started.md)

-   :material-file-document:{ .lg .middle } **TDN** — The Substrate

    ---

    TouchDesigner networks exported as human-readable JSON. The format is what lets your AI agent see what's on screen, what lets you diff one attempt against another, and what lets a network rebuild itself from text. TDN is what makes the rest of this possible.

    [:octicons-arrow-right-24: Learn about TDN](tdn/index.md)

</div>

---

## What You Can Do

You and your AI assistant, in the same session, with full context of your live network:

| | Capability | Example |
|---|-----------|---------|
| :material-plus-circle: | **Build entire networks from a sentence** | "Build me a noise-driven particle system." |
| :material-refresh: | **Try a different approach in seconds** | "Actually, make it react to audio instead." |
| :material-tune: | **Read & set any parameter** | "Set the noise frequency to match the audio input." |
| :material-code-braces: | **Write extensions** | "Create an extension class that manages scene transitions." |
| :material-bug: | **Debug errors** | "Why is my render chain producing a black output?" |
| :material-source-branch: | **Compare attempts side by side** | "Show me what changed between this version and the last one." |
| :material-magnify: | **Inspect anything** | "What parameters are non-default on this operator?" |
| :material-annotation: | **Document networks** | "Add annotations to group and label these operators." |
| :material-test-tube: | **Run tests** | "Run the test suite and fix any failures." |

You describe what you want. The AI works with your live network — operators, connections, parameters, hierarchy — with the whole picture. The result is a network you can read, revert, and rebuild from text.

[:octicons-arrow-right-24: Full tool reference](envoy/tools-reference.md)

---

## Key Features

| | Feature | Description |
|---|---------|-------------|
| :material-sync: | **Automated Externalization** | Tags COMPs and DATs, keeps external files in sync — auto-restores from disk on project open |
| :material-robot: | **Envoy MCP Server** | 46 tools connect AI assistants to your live TD session |
| :material-file-document: | **TDN Format** | Export/import operator networks as diffable JSON for code review and snapshots |
| :material-keyboard: | **Keyboard Shortcuts** | Double-tap ++lctrl++ to tag, ++ctrl+shift+u++ to save — minimal friction |
| :material-cog: | **Parameter Tracking** | Automatically detects parameter changes and marks COMPs dirty |
| :material-test-tube: | **41 Test Suites** | Comprehensive automated testing framework |
| :material-note-text: | **Structured Logging** | Multi-destination logging with file rotation, ring buffer, and MCP access |

---

## Requirements

- **TouchDesigner 2025.32280** or later (Windows / macOS)
- A **git repository** is optional. Embody works in any project folder; if you happen to use git, every change is also a clean diff for free.

---

## Quick Start

1. **Download** the Embody `.tox` from the [release folder](https://github.com/dylanroscover/Embody/tree/main/release)
2. **Drag and drop** it into your TouchDesigner project
3. **Enable Envoy** to connect AI assistants to your session
4. **Tag operators** by pressing ++lctrl++ twice on any COMP or DAT
5. **Save** with ++ctrl+shift+u++ — on next project open, everything restores from disk automatically

[:octicons-arrow-right-24: Full setup guide](embody/getting-started.md)
