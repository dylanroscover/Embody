"""Rebuild public/llms-full.txt from the docs it inlines. Run after a docs change:

    python platform/apps/web/scripts/build-llms-full.py

A machine file, so ASCII only (.claude/rules/ascii-punctuation.md): an unmapped
character stops the build instead of reaching a reader as mojibake.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
OUT = ROOT / "platform/apps/web/public/llms-full.txt"
RULE = "=" * 78

SECTIONS = (
    ("Quickstart", "docs/quickstart.md", ""),
    ("Envoy - Setup", "docs/envoy/setup.md", ""),
    ("Envoy - Claude Code Integration", "docs/envoy/claude-code.md", ""),
    ("Envoy - Tools Reference", "docs/envoy/tools-reference.md",
     " (the {tools} MCP tools + {meta} bridge meta-tools)"),
    ("Convoy - LAN Relay", "docs/convoy/index.md", ""),
    ("TDXN - Specification", "docs/tdxn/specification.md", ""),
    ("TDXN - Import & Export", "docs/tdxn/import-export.md", ""),
    ("AI Instructions (AGENTS.md)", "AGENTS.md",
     " - critical rules for operating in a TD project"),
)

ASCII = {
    "—": "-", "–": "-", "‒": "-", "‑": "-", "−": "-",
    "…": "...", "→": "->", "←": "<-", "↔": "<->",
    "⇒": "=>", "×": "x", "‘": "'", "’": "'", "“": '"',
    "”": '"', "•": "-", "·": "-", "≥": ">=", "≤": "<=",
    " ": " ", "─": "-", "│": "|", "├": "+", "└": "\\",
    "✓": "yes", "✗": "no", "≈": "~", "°": " deg",
}


def ascii_only(text, source):
    text = "".join(ASCII.get(ch, ch) for ch in text.replace("\r\n", "\n"))
    bad = sorted({ch for ch in text if ord(ch) > 127})
    if bad:
        sys.exit("%s: add to ASCII: %s" % (source, [hex(ord(ch)) for ch in bad]))
    return text.strip() + "\n"


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def main():
    envoy = read("docs/envoy/index.md")
    tools = re.search(r"\*\*(\d+) MCP tools\*\*", envoy).group(1)
    meta = re.search(r"plus (\d+) bridge meta-tools", envoy).group(1)
    floor = re.search(r"MIN_TD_BUILD\s*=\s*['\"]([\d.]+)['\"]",
                      read("platform/apps/web/src/lib/tdBuild.ts")).group(1)
    lines = [
        "# Embody + Envoy + TDXN - full text for AI agents",
        "",
        "> Open-source TouchDesigner toolkit. Envoy is an MCP server (%s tools) that lets AI" % tools,
        "> assistants build, wire, parameterize, and debug a LIVE TouchDesigner session; Embody",
        "> externalizes the network to version-control-friendly text on disk; TDXN is the diffable",
        "> YAML network format that makes both possible; Convoy relays the work to other computers",
        "> on a trusted LAN. Free, MIT, runs entirely on the user's machine. Requires",
        "> TouchDesigner %s+ (Windows/macOS)." % floor,
        "",
        "This file inlines the essential documentation in one place so an agent can read it in a",
        "single fetch. Curated index: https://embody.tools/llms.txt  -  For agents: https://embody.tools/for-ai",
        "Repo: https://github.com/dylanroscover/Embody  -  Docs: https://dylanroscover.github.io/Embody/",
        "",
        "Sections below, in order:",
    ]
    for i, (title, _, note) in enumerate(SECTIONS, 1):
        lines.append("  %d. %s%s" % (i, title, note.format(tools=tools, meta=meta)))
    text = "\n".join(lines) + "\n"
    for title, source, _ in SECTIONS:
        text += "\n\n%s\n# %s\n# source: %s\n%s\n\n%s" % (
            RULE, title, source, RULE, ascii_only(read(source), source))
    OUT.write_bytes(text.encode("ascii"))
    print("%s: %d bytes, %d sections" % (OUT.name, len(text), len(SECTIONS)))


if __name__ == "__main__":
    main()
