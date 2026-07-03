# Quickstart

**From nothing to your first AI-built network in about five minutes.**

The fastest way to experience Embody is the AI way: install one thing, drag in one file, click one button, then *talk* to TouchDesigner and watch it build. You describe what you want in plain language — no coding, no scripting, and git is entirely optional.

Embody itself is free, open source (MIT), and runs entirely on your own machine — no Embody account, no subscription, nothing SaaSy. You'll sign in to your AI assistant (a Claude account for Claude Code, for example), but that's the only login involved. The server Embody starts is local-only; nothing about your project leaves your computer unless you choose to share it.

---

## Step 1 — Get the two prerequisites

You need two things installed before you start:

- **TouchDesigner 2025.32280 or later** — Windows or macOS. [Download from Derivative](https://derivative.ca/download).
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

As soon as Embody finishes initializing, it asks whether you want to set up **Envoy** — its AI bridge. A dialog appears asking **"Enable Envoy?"** with two buttons, **Skip** and **Enable Envoy**. Click **Enable Envoy**.

That single click does everything for you:

- Installs the MCP server's Python dependencies (~30 MB — TouchDesigner goes unresponsive for a few seconds while this runs)
- Starts a local server on `localhost:9870`
- Writes the AI config files into your project root — `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, and a `.claude/` folder of rules and skills — so your assistant knows how to talk to TouchDesigner

Clicked **Skip**, or want to turn Envoy off later? Toggle the **Envoy Enable** parameter on the Embody component anytime.

!!! note "If it asks about git"
    Don't use version control? After you click Enable Envoy, you may see a second dialog recommending a git repo. Among its options, click **Start Without Git** (avoid **Cancel** — that stops the setup). The AI config files are generated either way, and everything works the same.

!!! info "Local and private by default"
    Envoy only listens on your own computer (`127.0.0.1`) — it's not reachable from the internet or your network. Every Envoy tool is pre-authorized, too, so you're not stuck clicking through permission prompts.

---

## Step 5 — Open your AI assistant and start talking

**Keep TouchDesigner open** — your AI assistant talks to the live session. Now open your AI assistant in the **same folder as your `.toe`** and start a new chat:

- **Claude Code** (the fully auto-configured path): open that folder — `File → Open Folder` in the desktop or VS Code app, or `cd` into it and run `claude` in a terminal — then start a session. It detects the `.mcp.json` Envoy generated and connects on its own.
- **Cursor or Windsurf**: open the same folder; you may need to point it at the generated `.mcp.json` yourself — see [Envoy Setup](envoy/setup.md#manual-configuration).

!!! tip "One-click launch"
    Set the **AI Client** menu on the Embody component to your assistant, then pulse the **Launch AI Client** parameter — Embody opens the client (an editor as a workspace, or a CLI in a new terminal) already pointed at your project root.

Now just say what you want. Try this:

```text
Build me a noise-driven particle system.
```

Watch the operators appear in your live session — wired up, named, annotated, and laid out. Then keep going, conversationally:

```text
Now make it react to audio, slow it down, and add a bloom on the final output.
```

You're not getting a screenshot or a code snippet to paste. You're getting the **actual network**, in front of you, ready to play with.

!!! tip "Confirm the connection"
    If your assistant doesn't seem to see TouchDesigner, ask it to *"list all operators in the project."* If that comes back empty or errors, check that **Envoy Enable** is on and that you started the AI session **after** enabling it. Still stuck? See [Envoy Troubleshooting](envoy/troubleshooting.md).

---

## What you got for free

While you were building, Embody was quietly doing the other half of its job: **every operator can be saved to disk as readable text**. That means every version of your network is something you can diff, review, restore, and hand back to the AI later — no binary black box, no lock-in.

To tag an operator for externalization, select it and press ++lctrl++ twice. To save your changes, press ++ctrl+shift+u++. On the next project open, everything restores from disk automatically.

That's the whole loop — generate, compare, revert, branch — and it all runs at the speed of typing.

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
