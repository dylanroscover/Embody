# Embody — Submission Copy & Promotion Checklist

Ready-to-paste text for each platform. Tailor as needed.

---

## Short Description (1 line)

> MCP server for TouchDesigner — 45 tools let AI assistants create operators, set parameters, wire connections, and manage TD projects through natural conversation.

## Medium Description (1 paragraph)

> Embody is a TouchDesigner extension and MCP server that automates externalization of COMPs and DATs for version control, and enables AI agents like Claude to have a live conversation with TouchDesigner. Tag any operator, save your project, and Embody mirrors your network to version-control-friendly files on disk. Via its embedded MCP server (Envoy), AI assistants can query project state, create and connect operators, set parameters, write extensions, and debug errors — all through natural language. 45 MCP tools across 10 categories. Supports Windows and macOS.

---

## Hacker News — Show HN

**Title:** `Show HN: Embody – An MCP server that lets Claude talk to TouchDesigner`

**Body:**

I built an MCP server that gives AI coding assistants direct access to live TouchDesigner sessions. TouchDesigner is a visual programming environment used for real-time graphics, projection mapping, and interactive installations — but its projects are binary .toe files that are impossible to diff, merge, or review in git.

Embody started as an externalization tool — it mirrors your TD operators to readable files on disk (.py, .json, .tox, and a custom JSON format called TDN). Tag an operator, save your project, and Embody keeps everything in sync. On project open, it restores everything from disk automatically.

The real unlock came when I added Envoy, an embedded MCP server with 45 tools. Now Claude Code (or Cursor, Windsurf, or any MCP client) can create operators, wire connections, set parameters, write extensions, and debug errors inside a running TD session — all through conversation. It auto-generates .mcp.json and Claude Code configuration files so setup is: enable Envoy, open a terminal, start coding.

Repo: https://github.com/dylanroscover/Embody
Docs: https://dylanroscover.github.io/Embody/
License: TEC Friendly (open source)

---

## Reddit Posts

### r/ClaudeAI

**Title:** I built an MCP server that lets Claude control TouchDesigner in real-time

**Body:**

I've been building Embody, a TouchDesigner extension with an embedded MCP server called Envoy. It exposes 45 tools that let Claude Code (or any MCP client) interact with a live TD session — creating operators, wiring connections, setting parameters, writing extensions, debugging errors, all through natural conversation.

The workflow is: enable Envoy in TD, open Claude Code in your project folder, and start talking. Claude can see your network, create new operators, connect them, set parameters with expressions, scaffold entire extensions with proper boilerplate, and check for errors — without you touching the TD interface.

It also handles externalization — mirroring TD operators to diffable files on disk for version control. TD projects are binary .toe files that can't be diffed or merged, so this is a big deal for anyone collaborating on TD projects.

- 45 MCP tools across 10 categories
- Works with Claude Code, Cursor, Windsurf
- Auto-configures .mcp.json and AI client files
- Windows and macOS

Repo: https://github.com/dylanroscover/Embody
Docs: https://dylanroscover.github.io/Embody/

### r/LocalLLaMA

**Title:** Open-source MCP server for TouchDesigner — 45 tools for AI-assisted creative coding

**Body:**

Sharing an MCP server I built for TouchDesigner (real-time visual programming for installations, projection mapping, live visuals). The server exposes 45 tools that let any MCP-compatible client interact with a running TD session.

The MCP server (called Envoy) lets AI assistants create operators, set parameters, wire connections, read/write DAT content, scaffold extensions, export networks to JSON, and run arbitrary Python in TD. It uses FastMCP with HTTP transport + a STDIO bridge for CLI clients.

The broader project (Embody) also automates externalization of TD's binary .toe files to diffable text formats — Python files, JSON, and a custom network format called TDN.

It's open source under the TEC Friendly License. Works with Claude Code, Cursor, Windsurf, or any MCP client.

- Repo: https://github.com/dylanroscover/Embody
- Docs: https://dylanroscover.github.io/Embody/
- Tool reference: https://dylanroscover.github.io/Embody/envoy/tools-reference/

### r/touchdesigner

**Title:** Embody v5 — externalize your TD projects + let AI assistants talk to TouchDesigner via MCP

**Body:**

Hey everyone — I've been working on Embody for a while now and wanted to share where it's at. It started as a fork of Tim Franklin's External Tox Saver, rebuilt from the ground up.

**The externalization problem:** TD projects are binary .toe files. You can't diff them, can't merge them, can't review changes in a PR. Embody mirrors your operators to files on disk — .tox for COMPs, .py for DATs, and a new JSON format called TDN that captures entire networks (operators, connections, parameters, positions, annotations) in a human-readable, diffable format. Tag operators with a double-tap of left ctrl, update with Ctrl+Shift+U. On project open, everything restores from disk automatically.

**The MCP server:** Embody now includes Envoy, an embedded MCP server that lets AI coding assistants (Claude Code, Cursor, Windsurf) interact with your live TD session. 45 tools let the AI create operators, wire connections, set parameters, write extensions, export networks, and debug errors. The setup is: toggle Envoy on, open a terminal in your project folder, start talking. It auto-generates all the config files.

- 39 test suites
- Manager UI for tracking externalized operators
- Windows and macOS
- Open source (TEC Friendly License)

Repo: https://github.com/dylanroscover/Embody
Docs: https://dylanroscover.github.io/Embody/

Huge thanks to Tim Franklin for the original External Tox Saver, and to Elburz Sorkhabi, Matthew Ragan, and Wieland Hilker for inspiration and guidance.

### r/creativecoding

**Title:** Embody — version control + AI-assisted creative coding for TouchDesigner

**Body:**

TouchDesigner is a visual programming environment for real-time graphics, but its projects are binary files that can't be diffed or merged. I built Embody to fix that — it externalizes TD operators to readable files on disk, and includes an MCP server that lets AI assistants interact with running TD sessions through natural language.

The MCP server (Envoy) exposes 45 tools: create operators, wire connections, set parameters, write extensions, export networks to JSON. Works with Claude Code, Cursor, Windsurf, or any MCP client.

Repo: https://github.com/dylanroscover/Embody
Docs: https://dylanroscover.github.io/Embody/

### r/mediaarts

**Title:** Embody — externalize and AI-control TouchDesigner projects for installation and media art

**Body:**

For anyone working with TouchDesigner for installations, live performance, or media art — I built an extension called Embody that solves two problems:

1. **Version control**: TD projects are binary .toe files. Embody mirrors your operators to diffable files on disk, so you can track changes in git, collaborate on projects, and restore everything from disk on project open.

2. **AI-assisted development**: Embody includes an MCP server (Envoy) with 45 tools that let AI coding assistants like Claude interact with your live TD session. Create operators, set parameters, wire connections, debug errors — through natural conversation.

Useful for complex installations where you need version history and reproducibility, or for rapid prototyping where talking to an AI is faster than wiring by hand.

Repo: https://github.com/dylanroscover/Embody
Docs: https://dylanroscover.github.io/Embody/

---

## Derivative Forum

**Title:** Embody v5 — externalize your TD projects + AI-assisted development via MCP

**Body:**

Hi everyone,

I've been working on Embody, which grew out of Tim Franklin's External Tox Saver. It's been completely rebuilt and expanded with two major capabilities:

### Externalization

Tag any COMP or DAT (double-tap left ctrl) and Embody mirrors it to a file on disk. COMPs externalize as .tox files or as .tdn (a JSON format that captures the full network — operators, connections, parameters, positions, annotations). DATs externalize as .py, .json, .glsl, etc. Update with Ctrl+Shift+U, and on project open, everything restores from disk automatically.

This means you can:
- Diff changes between versions in git or any text tool
- Collaborate on TD projects with merge-friendly files
- Review structural changes in pull requests
- Restore your entire project from externalized files alone

### Envoy MCP Server

Embody now includes an embedded MCP server called Envoy. When enabled, AI coding assistants like Claude Code, Cursor, or Windsurf can talk directly to your running TD session. 45 tools let the AI:

- Create and delete operators
- Set parameters (values, expressions, binds)
- Wire and disconnect connections
- Read and write DAT content
- Scaffold extensions with proper boilerplate
- Export and import networks as JSON
- Check for errors and debug problems

Setup: toggle Envoy on in the Embody COMP parameters, open a terminal in your project folder, and start a conversation. Envoy auto-generates all the config files your AI client needs.

### Details

- 39 test suites, 587 test methods
- Manager UI for tracking all externalized operators
- Windows and macOS
- TDN network format with full specification
- Comprehensive documentation: https://dylanroscover.github.io/Embody/

Download the .tox from the release page: https://github.com/dylanroscover/Embody/releases

Thanks to Tim Franklin for the original foundation, and to Elburz, Matthew Ragan, and Wieland Hilker for ongoing inspiration and guidance.

---

## Product Hunt

**Tagline:** Have a conversation with TouchDesigner

**Description:** Embody is a TouchDesigner extension with an embedded MCP server that lets AI assistants create operators, set parameters, wire connections, and manage TD projects through natural conversation. It also automates externalization of TD's binary project files to version-control-friendly formats. 45 MCP tools, 39 test suites, Windows and macOS.

**Maker comment:** TouchDesigner projects are binary .toe files — impossible to diff or merge in git. I built Embody to externalize TD operators to readable files on disk, and then added an MCP server so AI assistants like Claude could talk directly to a running TD session. The result: you can version-control your creative coding projects AND have an AI help you build them.

---

## Twitter/X

> Embody v5 — an MCP server that lets @AnthropicAI Claude (and Cursor, Windsurf) talk to live @derivative01 TouchDesigner sessions. 45 tools: create ops, wire connections, set parameters, write extensions, debug errors. Plus automated externalization for version control.
>
> https://github.com/dylanroscover/Embody
>
> #MCP #TouchDesigner #CreativeCoding #ModelContextProtocol

---

## Manual Submission Checklist

### MCP Directories (web forms — requires login)
- [ ] **Official MCP Registry** — publish `server.json` via registry CLI. Requires GitHub auth for namespace `io.github.dylanroscover`. See: https://github.com/modelcontextprotocol/registry
- [ ] **mcpservers.org** — https://mcpservers.org/submit (for the wong2/awesome-mcp-servers list)
- [ ] **PulseMCP** — email hello@pulsemcp.com or https://www.pulsemcp.com/submit
- [ ] **mcp.directory** — check for a submit option
- [ ] **mcp.so** — check for a submit option

### TouchDesigner Community
- [ ] **Derivative Forum** — https://forum.derivative.ca — post in Share/Tools category (copy above)
- [ ] **Derivative Community Page** — https://derivative.ca/community — submit a community post
- [ ] **Interactive & Immersive HQ** — email Elburz Sorkhabi (interactiveimmersive.io) to pitch a feature or tools roundup inclusion
- [ ] **ChopChopChop** — submit for inclusion in their TD tools directory

### Social / Content
- [ ] **Hacker News** — https://news.ycombinator.com/submit — use the Show HN copy above
- [ ] **Reddit r/ClaudeAI** — post with MCP-first framing (copy above)
- [ ] **Reddit r/LocalLLaMA** — post with MCP/open-source framing (copy above)
- [ ] **Reddit r/touchdesigner** — post with externalization-first framing (copy above)
- [ ] **Reddit r/creativecoding** — post with creative-tech framing (copy above)
- [ ] **Reddit r/mediaarts** — post with installation/art framing (copy above)
- [ ] **Twitter/X** — tag @AnthropicAI, @derivative01 (copy above)
- [ ] **Anthropic Discord** — share in MCP-focused channels
- [ ] **Product Hunt** — launch Tue-Thu morning Pacific (copy above)

### Outreach
- [ ] **Anthropic MCP showcase** — tag or email Anthropic about Embody as an MCP implementation
- [ ] **YouTube** — record a short (<60s) demo of the MCP workflow (Claude actually driving TD)
- [ ] **Blog post / Dev.to** — "How I made TouchDesigner talk to Claude via MCP" — cross-post link to HN and Reddit
- [ ] **TD meetups** — TouchDesigner Roundtable Berlin, DATLAB NYC (both accept remote presentations)

### Priority Order (highest reach-to-effort ratio)
1. Publish to Official MCP Registry (cascades to GitHub MCP Registry)
2. Submit to mcpservers.org + PulseMCP (web forms, 5 min each)
3. Awesome-list PRs (automated — check if merged)
4. Post on Hacker News (Show HN)
5. Post on Derivative Forum
6. Post on r/ClaudeAI and r/touchdesigner
