# Quickstart

**From nothing to your first AI-built network in about five minutes.**

The fastest way to experience Embody is the AI way: install one thing, drag in one file, click one button, then *talk* to TouchDesigner and watch it build. You describe what you want in plain language — no coding, no scripting, and git is entirely optional.

Embody itself is free, open source (MIT), and runs entirely on your own machine — no Embody account, no subscription, nothing SaaSy. You'll sign in to your AI assistant (a Claude account for Claude Code, for example), but that's the only login involved. The server Embody starts is local-only; nothing about your project leaves your computer unless you choose to share it.

---

## Step 1 — Get the two prerequisites

You need two things installed before you start:

- **TouchDesigner 2025.32820 or later** — Windows or macOS. [Download from Derivative](https://derivative.ca/download).
- **An AI assistant that speaks [MCP](https://modelcontextprotocol.io/)** (the open standard that lets AI tools drive other apps) — [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (recommended), [Codex](https://github.com/openai/codex), [Gemini](https://github.com/google-gemini/gemini-cli), [Cursor](https://www.cursor.com/), [Windsurf](https://windsurf.com/), or GitHub Copilot via VS Code.

!!! note "Never used an AI coding tool?"
    Claude Code is the gentlest place to start — it runs as a desktop app, a VS Code extension, or a web app, not only in a terminal. The first time, you'll create or sign in to a Claude account and then open a folder; you won't be writing code, just describing what you want in plain language. (The five-minute estimate above assumes TouchDesigner and your AI assistant are already installed — first-time installs take a little longer.)

---

## Step 2 — Download Embody

Grab the latest Embody `.tox` from **[GitHub Releases](https://github.com/dylanroscover/Embody/releases/latest)**. It's a single file.

---

## Step 3 — Drag it into your project

Open your `.toe` project (or a brand-new one) and **drag the `.tox` into the network**. That's the entire install — there's nothing else to set up.

Embody initializes itself automatically over the next couple of frames. The core externalization features are self-contained and need no external dependencies.

---

## Step 4 — Say yes to Envoy, the AI bridge

As soon as Embody finishes initializing, its [setup wizard](embody/setup-wizard.md) opens — a few quick screens, one decision each. For the fast path, take the recommended option on every screen: **Auto** mode, **Claude Code** as the assistant, **Don't ask** for permissions — then click **Set up Embody** on the summary. Nothing changes until that final click.

That single confirmation does everything for you:

- Installs the MCP server's Python dependencies (~30 MB, in the background — TouchDesigner stays responsive)
- Starts a local server on `127.0.0.1:9870`
- Writes the AI config files into your project root — `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, and a `.claude/` folder of rules and skills — so your assistant knows how to talk to TouchDesigner

Clicked **Not now**, or want to change a choice later? Pulse the **Setup Wizard** parameter on the Embody component to re-run it anytime, or toggle **Envoy Enable** directly.

!!! note "No git? No problem"
    The wizard doesn't require version control. The AI config files are generated either way — with a git repo Embody also adds its `.gitignore` / `.gitattributes` entries; without one it simply skips them.

!!! info "Local and private by default"
    Envoy only listens on your own computer (`127.0.0.1`) — it's not reachable from the internet or your network. Every Envoy tool is pre-authorized, too, so you're not stuck clicking through permission prompts.

---

## Step 5 — Open your AI assistant and start talking

**Keep TouchDesigner open** — your AI assistant talks to the live session.

The easiest way in is the **Launch AI Client** button: on the Embody component's **Envoy** parameter page, set the **AI Client** menu to your assistant, then click **Launch AI Client**. Embody opens the client (an editor as a workspace, or a CLI in a new terminal) already pointed at your project root — and if the client isn't installed yet, the terminal walks you through it: the official install command for your OS on its own line, ready to copy/paste.

Prefer to open it yourself? Open your AI assistant in the **same folder as your `.toe`** and start a new chat:

- **Claude Code** (the fully auto-configured path): open that folder — `File → Open Folder` in the desktop or VS Code app, or `cd` into it and run `claude` in a terminal — then start a session. It detects the `.mcp.json` Envoy generated and connects on its own.
- **Cursor or Windsurf**: open the same folder; you may need to point it at the generated `.mcp.json` yourself — see [Envoy Setup](envoy/setup.md#manual-configuration).

Now just say what you want. Try this:

```text
Build me a noise-driven particle system.
```

Watch the operators appear in your live session — wired up, named, annotated, and laid out. Then keep going, conversationally:

```text
Now make it react to audio, slow it down, and add a bloom on the final output.
```

You're not getting a screenshot or a code snippet to paste. You're getting the **actual network**, in front of you, ready to play with.

!!! info "How long does a build take?"
    Small things — a parameter change, a fix, a question about your network — can land in seconds. A complete network is a real build: the AI reads your project, plans, creates, wires, and verifies its work, which typically takes **5-20 minutes** of autonomous effort, depending on your model and compute. You don't have to watch it, and you don't have to run just one: Envoy [coordinates multiple AI sessions](envoy/multi-session.md) on the same project, each scoped to its own part of the network. Less like waiting on a genie, more like directing a team.

!!! tip "Confirm the connection"
    If your assistant doesn't seem to see TouchDesigner, ask it to *"list all operators in the project."* If that comes back empty or errors, check that **Envoy Enable** is on and that you started the AI session **after** enabling it. Still stuck? See [Envoy Troubleshooting](envoy/troubleshooting.md).

---

## What you got for free

Embody's other job is making your network **version-controllable**: any COMP or DAT can be saved to disk as readable text — something you can diff, review, restore, and hand back to the AI later. No binary black box, no lock-in.

Externalization is **opt-in** — nothing is written to disk until you choose. Two ways to opt in:

- **Tag operators yourself**: hover one and press ++lctrl++ twice.
- **Auto-externalize what the AI builds**: set the **Auto-Externalize New Ops** parameter (Embody component → **Envoy** page) to `DATs`, `COMPs`, or `DATs and COMPs`. New operators your assistant creates through Envoy are then tagged and externalized automatically. The default is `Neither`.

To save your changes, press ++ctrl+shift+u++. On the next project open, everything tagged restores from disk automatically.

That's the whole loop — generate, compare, revert, branch. The lateral moves run at the speed of typing; the generating you delegate and come back to.

!!! tip "Here for version control, not AI?"
    You can skip Envoy entirely and use Embody as a pure externalization engine — every operator diffable on disk, no AI involved. See [Getting Started](embody/getting-started.md) for that workflow.

---

## Where to go next

| If you want to… | Go here |
|---|---|
| Understand externalization in depth | [Getting Started](embody/getting-started.md) |
| See everything the AI can do | [Envoy Tools Reference](envoy/tools-reference.md) |
| Configure ports, multiple instances, or permissions | [Envoy Setup](envoy/setup.md) |
| Fix a connection problem | [Envoy Troubleshooting](envoy/troubleshooting.md) |
| Understand why this exists | [The Manifesto](manifesto.md) |

The tool keeps up with you, instead of the other way around. That's the whole idea.
