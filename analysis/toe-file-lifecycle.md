# How Embody Handles `.toe` Files

**Date:** 2026-07-08
**Project:** Embody — TouchDesigner externalization & AI-assisted development

---

## Core Principle: No Direct Binary Parsing

**Embody never directly opens, parses, or reads the binary `.toe` file.** There is zero code in the codebase that reads `.toe` bytes, calls `open()` on a `.toe`, or uses any library to decompile the TouchDesigner binary format.

All `.toe` interaction is delegated to TouchDesigner's own C++ engine. Embody operates entirely through TouchDesigner's runtime Python API on the *live* operator network that TD creates after deserializing the `.toe`.

---

## The Two Layers

```
┌─────────────────────────────────────────────────────────────┐
│                TouchDesigner Engine (C++)                   │
│  - Opens .toe binary deserialization                        │
│  - Creates live operator network in memory                  │
│  - Fires lifecycle callbacks (onStart, onSave, etc.)        │
│  - Serializes network back to .toe binary on save           │
└────────────────────────┬────────────────────────────────────┘
                         │  TD Python API
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                Embody Extension (Python)                    │
│  - Accessses live ops via op(), findChildren(), par.eval()  │
│  - Reads/writes .tdn, .tsv, .json, .py text files           │
│  - Hooks into lifecycle callbacks via Execute DAT           │
│  - NEVER opens the .toe binary                              │
└─────────────────────────────────────────────────────────────┘
```

---

## File Lifecycle

### Opening a Project

0. The user or `envoy_bridge.py` launches TouchDesigner with a `.toe` path.
1. TD's engine deserializes the `.toe` binary into a live operator network.
2. Embody's `execute.py` `onStart()` fires at frame 0.
3. A multi-frame cascade restores externalized state from text files:

| Frame | Action | File | What Happens |
|-------|--------|------|-------------|
| 0 | `init()` | `execute.py:34` | Resets runtime state (Envoystatus, Performmode) |
| 5 | `_restoreSettings()` | `EmbodyExt.py` | Reads `.embody/config.json` — user preferences |
| 10 | `EnsureCatalogs()` | `CatalogManagerExt.py` | Loads `.embody/catalog_<build>.json` operator catalogs |
| 45 | `RestoreTOXComps()` | `EmbodyExt.py` | For missing TOX COMPs: reads `externalizations.tsv`, creates shell, sets `externaltox` → TD auto-loads `.tox` |
| 50 | `RestoreDATs()` | `EmbodyExt.py` | For missing DATs: reads `externalizations.tsv`, creates DAT, sets `file`/`syncfile` |
| 60 | `ReconstructTDNComps()` | `TDNExt.py` | For TDN COMPs: reads `.tdn` YAML files, calls `ImportNetwork(clear_first=True)` via TD API |
| 75 | `ReconcileMetadata()` | `EmbodyExt.py` | Re-applies tags, colors, file-parameter bindings |
| 80 | `_writeProjectJson()` | `EmbodyExt.py` | Pins TD build to `.embody/project.json` |
| 90 | `RecoverOrphanShells()` | `EmbodyExt.py` | Finds TDN-tagged empty COMPs with lost TSV rows but intact `.tdn` |

### Saving a Project

When the user presses Ctrl+S (or `project.save()` fires):

```
┌──────────────────────────────────────────────────────────────┐
│  onProjectPreSave()  (execute.py:100)                        │
│                                                              │
│  1. Clean up runtime-only storage                            │
│  2. Update externalization state (Update)                    │
│  3. Export children to .tdn files (TDN.ExportNetwork)        │
│  4. [Optional] Strip children from live network              │
│     (StripCompChildren — keeps .toe small)                   │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  TD Engine serializes live network to .toe binary on disk    │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  onProjectPostSave()  (execute.py:306)                       │
│                                                              │
│  1. Re-import stripped children from .tdn (ImportNetwork)    │
│  2. Restore pane navigation                                  │
│  3. Update .embody/project.json                              │
│  4. Refresh envoy.json registry (handle filename version)    │
│  5. Restart Envoy MCP server if strip tore it down           │
└──────────────────────────────────────────────────────────────┘
```

---

## Key Files Involved

| File | Role |
|------|------|
| `dev/embody/Embody/execute.py` | Lifecycle callbacks — `onStart`, `onCreate`, `onProjectPreSave`, `onProjectPostSave` |
| `dev/embody/Embody/EmbodyExt.py` | Core externalization engine — reconstruction cascade, TOX/DAT/TDN restoration |
| `dev/embody/Embody/TDNExt.py` | TDN format export/import — reads/writes `.tdn` YAML files via `ExportNetwork`/`ImportNetwork` |
| `dev/embody/envoy_bridge.py` | External bridge process — launches TD with `.toe` CLI arg, never reads the file |
| `dev/embody/externalizations.tsv` | Central tracking table — maps operators to externalized file paths and strategies |
| `dev/embody/Embody/EnvoyExt.py` | MCP server — provides tools that read/write the live network through TD Python API |
| `dev/embody/Embody/envoy_setup.py` | Manages envoy.json instance registry (resolves `.toe` filename version bumps) |

---

## What Reads What

| Operation | How It's Done | Reads Binary .toe? |
|-----------|--------------|-------------------|
| Opening a `.toe` | TD's own C++ engine deserializes it | Yes (native) |
| Reading network structure | `op()`, `findChildren()`, `par.eval()` on live operators | No — TD Python API |
| Externalizing to `.tdn` | `TDNExt.ExportNetwork()` iterates live operators, serializes to YAML | No — pure dict work |
| Reconstructing from `.tdn` | `TDNExt.ImportNetwork()` calls `create()`, `.par.val =`, etc. | No — TD Python API |
| Restoring TOX COMPs | Creates COMP, sets `externaltox`, TD auto-loads `.tox` | No — TD auto-loads |
| Restoring DATs | Creates DAT, sets `file`/`syncfile`, TD auto-syncs from disk | No — TD auto-syncs |
| Saving a `.toe` | TD's engine serializes the live network | Yes (native) |
| Tracking externalized ops | Reads/writes `externalizations.tsv` (plain text TSV) | No — text file |
| Config/preferences | Reads `.embody/config.json`, `.embody/project.json` | No — text files |
| Operator catalogs | Reads `.embody/catalog_<build>.json` | No — text files |

---

## The `.toe` Filename Version Bump

TouchDesigner increments the `.toe` filename on each save:
- `Embody-5.398.toe` → `Embody-5.399.toe`

The bridge handles this through:
- **`find_latest_versioned_toe()`** (`envoy_bridge.py:328`): Strips trailing digits, scans directory for siblings matching the prefix, returns highest suffix.
- **`RefreshRegistry()`** (`envoy_setup.py`): Called from `onProjectPostSave` to update the instance registry with the new basename.
- **`Update()` backstop** (`EmbodyExt.py`): Watches `project.name` post-save and triggers registry refresh as a safeguard.

---

## Summary

| Layer | Tool | .toe Access |
|-------|------|-------------|
| **TouchDesigner Engine** | Native C++ | Loads/saves binary `.toe` directly |
| **envoy_bridge.py** | External Python proc | Passes path as CLI arg — never reads file |
| **execute.py** | TD Execute DAT | Hooks lifecycle callbacks — no binary access |
| **EmbodyExt.py** | TD Extension | Reconstructs from `.tdn`/`.tox`/`.tsv` text files — no binary access |
| **TDNExt.py** | TD Extension | Exports/imports `.tdn` YAML — no binary access |

Embody's actual data format is **`.tdn` (YAML)**, not `.toe`. The `.toe` is the opaque container; all meaningful parsing and diffing happens on text files.
