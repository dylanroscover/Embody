---
hide:
  - navigation
---

# Create at the Speed of Thought

Everything is moving faster now. Everyone is shipping. We're entering a brand new era of creativity, and Embody exists to help our community build faster and better than ever — open source, no strings attached, nothing SaaSy.

Most ideas die in the gap between imagining them and seeing them on screen. Trying the idea costs more than moving on to something safer, so people move on. Embody is built to make trying cheap.

**Envoy** is an MCP server that lets your AI assistant work inside your live TouchDesigner session. You describe what you want, and the operators appear in your network: wired, named, laid out, annotated. Not a screenshot of a network, not a code snippet to paste. The real thing, in front of you, ready to play with.

The honest numbers: a small change lands in seconds. A complete network takes the agent ten to twenty minutes, because it reads your network, plans, builds, and checks its own work. What changes is that you don't have to sit through it, and you don't have to run just one. Envoy coordinates several AI sessions on the same project. Each one claims a part of the network, sees what the others are doing, and is stopped from colliding with them. You direct. They build in parallel.

A tool that only builds forward is a trap. You ask for a thing, you get a thing, and now you're stuck with it, because trying something else means starting over. **Embody** is the other half. Tag any operator and it lives on disk as a file. Branch off the version that was almost right. Restore yesterday's state on the next project open. See exactly what changed since last week. Hand the agent what's on screen right now and ask for a variation. Every direction you'd want to move costs about as much as deciding to move there.

Both halves depend on **TDXN**, a text format the network can live in. A `.toe` file is opaque: your agent can't read it, your diff tool can't compare it, and git can't keep two versions of it side by side. TDXN writes a network as one YAML file, operators, parameters, connections, layout, annotations, DAT content, all of it. With the network in text, compare, revert, and branch run at the speed of typing, and a build becomes something you can hand off and come back to.

The promise is simple: the tool keeps up with you, instead of the other way around. You stay with the idea. The agents do the translation, in parallel.

Embody is open source, runs entirely inside TouchDesigner, and works with Claude Code, Codex, Gemini CLI, Cursor, Windsurf, GitHub Copilot via VS Code, or any MCP-compatible client. Pick an idea you've been putting off and see how far you get in an hour.

— Dylan Roscover
