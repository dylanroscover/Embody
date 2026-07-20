#!/usr/bin/env python3
"""Git textconv driver for Embody .tdn files.

Makes `git diff`, `git log -p`, and `git show` on .tdn files show only
SEMANTIC network changes. Every .tdn export writes a volatile header --
build number, export timestamp, Embody/TD version, source .toe name --
that changes without the network changing. Without this driver, a plain
re-export produces a noisy diff (timestamp churn) that buries real edits;
with it, re-exporting an unchanged network produces an EMPTY diff.

This is the committed/on-disk counterpart to the live `diff_tdn` MCP tool:
diff_tdn shows UNSAVED changes (live TD network vs the on-disk .tdn) that
git cannot see; this driver makes git's view of the on-disk .tdn (working
tree, history) clean. Together they cover the whole timeline.

Configured via:
    .gitattributes:  *.tdn diff=tdn
    git config:      diff.tdn.textconv = python3 <this script>

Git invokes it as `<textconv> <path-to-blob>` for each side of a diff and
compares the stdout. It reads BOTH legacy JSON .tdn (history blobs) and
v2.0 YAML .tdn (working tree), normalizes them identically, and re-emits
v2.0 YAML so a cross-format diff (JSON history vs YAML working tree) of an
unchanged network is empty. Requires PyYAML (NOT stdlib in a bare git/CI
python); on any `import yaml` failure OR read/parse error it emits the raw
file unchanged, so a malformed .tdn still diffs (unfiltered) rather than
breaking git.

The stripped key set is a deliberate SUPERSET of TDNExt._TDN_VOLATILE_KEYS:
it also drops 'version' and 'source_file' so a v1.5-JSON-history blob and a
v2.0-YAML-working-tree blob of the same network normalize identically. The
driver list is local; it does NOT touch the on-disk version field.
"""

import json
import sys

try:
    import yaml
    _HAVE_YAML = True
except Exception:
    _HAVE_YAML = False

# Header keys written into every .tdn on export that change without the
# network changing. Deliberate SUPERSET of TDNExt._TDN_VOLATILE_KEYS
# ({'build','generator','td_build','exported_at'}): 'version' is added so the
# v1.5->v2.0 format bump does not churn the diff, and 'source_file' is dropped
# across the migration boundary. Do NOT 'sync' this to equality with
# _TDN_VOLATILE_KEYS -- the broader set is correct by intent.
VOLATILE_KEYS = ('build', 'generator', 'td_build', 'exported_at',
                 'source_file', 'version')


if _HAVE_YAML:
    # CSafe is faster and reads legacy tab-indented JSON .tdn that the
    # pure-python SafeLoader rejects; fall back to pure-python Safe* only if
    # libyaml is absent.
    try:
        _BaseDumper = yaml.CSafeDumper
        _BaseLoader = yaml.CSafeLoader
    except AttributeError:
        _BaseDumper = yaml.SafeDumper
        _BaseLoader = yaml.SafeLoader

    class _TDNYamlDumper(_BaseDumper):
        """Private subclass so TDN representers never leak into SafeDumper."""
        pass

    def _tdn_str_representer(dumper, data):
        style = '|' if '\n' in data else None
        return dumper.represent_scalar('tag:yaml.org,2002:str', data,
                                       style=style)

    def _tdn_list_representer(dumper, data):
        flow = (len(data) <= 4
                and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                        for x in data))
        return dumper.represent_sequence('tag:yaml.org,2002:seq', data,
                                         flow_style=flow)

    _TDNYamlDumper.add_representer(str, _tdn_str_representer)
    _TDNYamlDumper.add_representer(list, _tdn_list_representer)


def _parse(raw):
    """Parse a .tdn document. JSON-first (legacy tab-indented JSON), else YAML.

    Mirrors TDNExt.tdn_load: feeds json.loads the BOM/whitespace-stripped text
    so a BOM-prefixed legacy JSON blob does not fall through to a YAML parse
    that would ScannerError on the tab indentation.
    """
    stripped = raw.lstrip('﻿').lstrip()
    if stripped[:1] in ('{', '['):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return yaml.load(raw, Loader=_BaseLoader)


def _normalize_dat_content(node):
    """Convert v1.5 array-of-lines dat_content to a plain string in place.

    Recurses through the operators tree so both diff sides serialize the same
    multi-line script form. A list joined with '\\n' matches what v2.0 stores
    and what _setDATContent rejoins on import (lossless).
    """
    if isinstance(node, dict):
        if (node.get('dat_content_format') == 'text'
                and isinstance(node.get('dat_content'), list)):
            node['dat_content'] = '\n'.join(node['dat_content'])
        for value in node.values():
            _normalize_dat_content(value)
    elif isinstance(node, list):
        for item in node:
            _normalize_dat_content(item)


def normalize(raw):
    """Return raw .tdn text with volatile header keys removed, re-emitted as
    v2.0 YAML.

    Falls back to the unmodified input if PyYAML is unavailable or the text is
    not parseable, so the driver never makes a diff worse than the unfiltered
    default.
    """
    if not _HAVE_YAML:
        return raw
    try:
        doc = _parse(raw)
    except Exception:
        return raw
    if isinstance(doc, dict):
        for key in VOLATILE_KEYS:
            doc.pop(key, None)
        _normalize_dat_content(doc)
    # Deterministic, order-preserving dump with the SAME config as TDNExt's
    # _TDNYamlDumper (block scalars, short-numeric list flow, sort_keys=False).
    # Both sides of a diff pass through this identical normalization.
    try:
        out = yaml.dump(doc, Dumper=_TDNYamlDumper, sort_keys=False,
                        width=4096, allow_unicode=True)
    except Exception:
        return raw
    return out if out.endswith('\n') else out + '\n'


def main(argv):
    if len(argv) < 2:
        return 0
    # Git runs this with stdout in the console codepage (cp1252 on Windows);
    # .tdn is UTF-8 and user networks legitimately contain non-ASCII (button
    # labels, annotations). Reconfigure so unicode never crashes the diff.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    try:
        with open(argv[1], 'r', encoding='utf-8') as f:
            raw = f.read()
    except OSError:
        return 0
    sys.stdout.write(normalize(raw))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
