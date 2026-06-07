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
compares the stdout. Pure stdlib (no TouchDesigner imports) so it runs in
any git context (hooks, CI, a bare checkout). On any read/parse error it
emits the raw file unchanged, so a malformed .tdn still diffs (unfiltered)
rather than breaking git.

The stripped key set is kept in sync with TDNExt._TDN_VOLATILE_KEYS -- the
same keys diff_tdn ignores for its comparison.
"""

import json
import sys

# Header keys written into every .tdn on export that change without the
# network changing. Keep in sync with TDNExt._TDN_VOLATILE_KEYS.
VOLATILE_KEYS = ('build', 'generator', 'td_build', 'exported_at', 'source_file')


def normalize(raw):
    """Return raw .tdn text with volatile header keys removed.

    Falls back to the unmodified input if it is not valid JSON, so the
    driver never makes a diff worse than the unfiltered default.
    """
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(doc, dict):
        for key in VOLATILE_KEYS:
            doc.pop(key, None)
    # Deterministic, order-preserving dump. Both sides of a diff pass through
    # this identical normalization, so the chosen formatting yields consistent
    # output and the diff reflects content differences only.
    return json.dumps(doc, indent='\t', ensure_ascii=False) + '\n'


def main(argv):
    if len(argv) < 2:
        return 0
    try:
        with open(argv[1], 'r', encoding='utf-8') as f:
            raw = f.read()
    except OSError:
        return 0
    sys.stdout.write(normalize(raw))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
