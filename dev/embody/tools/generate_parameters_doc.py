#!/usr/bin/env python3
"""Generate docs/embody/parameters.md from the externalized Embody.tdn.

The Embody COMP's custom parameters are the ground truth -- name, style, default,
help text, and menu options all live in dev/embody/Embody.tdn (the externalized
TDN of the Embody COMP itself). This script reads that file and emits a complete,
page-grouped Parameter Reference so the docs never drift from the actual COMP.

Regenerate after changing Embody's parameters (then commit the result):

    python dev/embody/tools/generate_parameters_doc.py

It writes docs/embody/parameters.md. ASCII punctuation only (repo rule).
"""
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TDN_PATH = os.path.join(REPO, "dev", "embody", "Embody.tdn")
OUT_PATH = os.path.join(REPO, "docs", "embody", "parameters.md")


# Normalize non-ASCII punctuation to ASCII (repo rule: generated files stay ASCII).
_ASCII = {
    0x2014: "-", 0x2013: "-", 0x2012: "-", 0x2015: "-",  # dashes
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",  # single quotes
    0x201C: '"', 0x201D: '"', 0x201E: '"',               # double quotes
    0x2026: "...", 0x2022: "*", 0x00A0: " ",              # ellipsis, bullet, nbsp
    0x2192: "->", 0x2190: "<-", 0x2194: "<->", 0x00D7: "x",
    0x2265: ">=", 0x2264: "<=",
}


def esc(value):
    """Make a value safe for a single Markdown table cell (ASCII punctuation)."""
    text = "" if value is None else str(value)
    text = text.translate(_ASCII)
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def fmt_default(par):
    """Human-readable default for a parameter, by style."""
    style = par.get("style", "")
    if style == "Pulse" or style == "Header":
        return "-"
    if "default" in par:
        default = par["default"]
    elif "value" in par:
        default = par["value"]
    else:
        return "-"
    if isinstance(default, bool):
        return "On" if default else "Off"
    if default == "":
        return "*(empty)*"
    return "`%s`" % esc(default)


def fmt_type(par):
    style = par.get("style", "") or "-"
    if par.get("readOnly"):
        return "%s (read-only)" % style
    return style


def fmt_desc(par):
    parts = []
    help_text = par.get("help")
    if help_text:
        parts.append(esc(help_text))
    # Menu / string-menu options
    labels = par.get("menuLabels") or par.get("menuNames")
    if labels and par.get("style") in ("Menu", "StrMenu"):
        opts = ", ".join("`%s`" % esc(x) for x in labels)
        parts.append("Options: %s." % opts)
    return " ".join(parts) if parts else "-"


def par_name_cell(par):
    name = esc(par.get("name", ""))
    label = par.get("label")
    if label and label != par.get("name"):
        return "%s (`%s`)" % (esc(label), name)
    return "`%s`" % name


def main():
    with open(TDN_PATH, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)

    pages = doc.get("custom_pars")
    if not pages:
        sys.exit("No top-level custom_pars found in %s" % TDN_PATH)

    total = sum(len(p) for p in pages.values())

    lines = []
    lines.append("# Parameter Reference")
    lines.append("")
    lines.append(
        "Complete reference for every custom parameter on the **Embody** COMP, "
        "grouped by parameter page. For a guided tour of the settings that matter "
        "most, see [Configuration](configuration.md)."
    )
    lines.append("")
    lines.append(
        "<!-- GENERATED FILE - do not edit by hand. "
        "Regenerate with: python dev/embody/tools/generate_parameters_doc.py -->"
    )
    lines.append("")
    lines.append(
        "!!! info \"Auto-generated from `Embody.tdn`\""
    )
    lines.append(
        "    This page is generated from the externalized Embody COMP "
        "(`dev/embody/Embody.tdn`), the source of truth for its parameters, so it "
        "stays in sync with the actual component. **%d parameters** across %d pages."
        % (total, len(pages))
    )
    lines.append("")

    for page_name, params in pages.items():
        lines.append("## %s" % page_name)
        lines.append("")
        lines.append("| Parameter | Type | Default | Description |")
        lines.append("|---|---|---|---|")
        for par in params:
            lines.append(
                "| %s | %s | %s | %s |"
                % (
                    par_name_cell(par),
                    esc(fmt_type(par)),
                    fmt_default(par),
                    fmt_desc(par),
                )
            )
        lines.append("")

    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")

    print("Wrote %s (%d parameters, %d pages)" % (OUT_PATH, total, len(pages)))


if __name__ == "__main__":
    main()
