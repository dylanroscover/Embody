---
hide:
  - navigation
---

# Embody

### Supercharge Your TouchDesigner Workflow With AI

Build faster. Debug smarter. Let AI handle the tedious parts while you focus on what matters — your creative vision. Through the **Envoy MCP server**, you can direct AI assistants like Claude to create operators, wire connections, set parameters, write extensions, and debug errors, all inside your live TD session.

No copy-pasting code. No describing your network in chat. You stay in control while AI does the heavy lifting.

<div class="grid cards" markdown>

-   :material-robot:{ .lg .middle } **Envoy** — AI Meets TouchDesigner

    ---

    An embedded [MCP](https://modelcontextprotocol.io/) server with **40+ tools** that connects AI assistants like Claude Code, Cursor, and Windsurf to your live TD session. Create operators, set parameters, wire connections, write extensions, inspect errors, export networks — all through natural conversation.

    [:octicons-arrow-right-24: Setup Envoy](envoy/setup.md)

-   :material-sync:{ .lg .middle } **Embody** — Version Control for TD

    ---

    Tag any operator and Embody keeps external files (`.tox`, `.py`, `.json`, `.glsl`, etc.) in sync. On project open, everything is auto-restored from disk. Your entire network becomes diffable, mergeable, and reviewable in git.

    [:octicons-arrow-right-24: Get started](embody/getting-started.md)

-   :material-file-document:{ .lg .middle } **TDN** — Networks as JSON

    ---

    Export your operator network to human-readable JSON. Review structural changes in git diffs, snapshot configurations, and reconstruct entire networks from text.

    [:octicons-arrow-right-24: Learn about TDN](tdn/index.md)

</div>

---

## What You Can Do With Envoy

With Envoy running, you can direct an AI assistant to handle anything in TouchDesigner — programmatically:

| | Capability | Example |
|---|-----------|---------|
| :material-plus-circle: | **Create & connect operators** | "Build me a noise-driven particle system" |
| :material-tune: | **Read & set any parameter** | "Set the noise frequency to match the audio input" |
| :material-code-braces: | **Write extensions** | "Create an extension class that manages scene transitions" |
| :material-bug: | **Debug errors** | "Why is my render chain producing a black output?" |
| :material-export: | **Export & import networks** | "Export this COMP to TDN so I can review the diff" |
| :material-magnify: | **Inspect anything** | "What parameters are non-default on this operator?" |
| :material-annotation: | **Document networks** | "Add annotations to group and label these operators" |
| :material-test-tube: | **Run tests** | "Run the test suite and fix any failures" |

You describe what you want in plain language, and the AI works with your live network — operators, connections, parameters, hierarchy — with full context.

[:octicons-arrow-right-24: Full tool reference](envoy/tools-reference.md)

---

## Key Features

| | Feature | Description |
|---|---------|-------------|
| :material-robot: | **Envoy MCP Server** | 40+ tools connect AI assistants to your live TD session |
| :material-sync: | **Automated Externalization** | Tags COMPs and DATs, keeps external files in sync — auto-restores from disk on project open |
| :material-file-document: | **TDN Format** | Export/import operator networks as diffable JSON for code review and snapshots |
| :material-keyboard: | **Keyboard Shortcuts** | Double-tap ++lctrl++ to tag, ++ctrl+shift+u++ to save — minimal friction |
| :material-cog: | **Parameter Tracking** | Automatically detects parameter changes and marks COMPs dirty |
| :material-test-tube: | **30 Test Suites** | Comprehensive automated testing with 587 test methods |
| :material-note-text: | **Structured Logging** | Multi-destination logging with file rotation, ring buffer, and MCP access |

---

## Requirements

- **TouchDesigner 2025.32280** or later (Windows / macOS)
- A **git repository** containing your `.toe` project (recommended)

---

## Quick Start

1. **Download** the Embody `.tox` from the [release folder](https://github.com/dylanroscover/Embody/tree/main/release)
2. **Drag and drop** it into your TouchDesigner project
3. **Enable Envoy** to connect AI assistants to your session
4. **Externalize operators** by pressing ++lctrl++ twice on any COMP or DAT — tags and saves in one step
5. **Save as you work** with ++ctrl+shift+u++ — on next project open, everything restores from disk automatically

[:octicons-arrow-right-24: Full setup guide](embody/getting-started.md)
