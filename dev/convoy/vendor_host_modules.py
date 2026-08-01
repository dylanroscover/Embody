#!/usr/bin/env python3
"""Re-vendor the Convoy host app into the Embody .tox.

The host-app daemon lives here, in dev/convoy/. A SECOND copy lives at
dev/embody/Embody/convoy/host/ as text DATs inside the Embody component, so
that installing the host app works with no network access -- a show LAN or a
locked-down studio has no reliable connection at install time. That two-copy
tax is deliberate (see CONVOY_INSTALL_PLAN.md section 1.1, which rejected
downloading the daemon from the GitHub release for exactly that reason).

This script is what keeps the copies honest, and test_convoy_host_vendor.py is
what fails the build when someone forgets to run it.

    python dev/convoy/vendor_host_modules.py --check     # report drift, write nothing
    python dev/convoy/vendor_host_modules.py             # re-vendor

WHY A PLAIN FILE COPY IS ENOUGH: the vendored DATs are externalized with
syncfile=True, so TouchDesigner reloads a DAT the moment its file changes on
disk. Copying the file IS the re-vendor; TD picks it up with no scripting. The
new content is baked into the shipped .tox by the next project.save().

TouchDesigner must be running for the DATs to pick the change up live. If it is
not, the files are still correct on disk and TD syncs them on next open --
file beats the tox-embedded snapshot on every load path (TD 2025.32820+).

ADDING A MODULE TO THE DAEMON is not fully done until it is also a DAT: this
script copies the file, but a brand-new module still needs a text DAT created
in the `host` COMP and externalized, or it ships nowhere. The parity test names
exactly that case rather than letting it pass silently.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
VENDOR_REL = os.path.join('dev', 'embody', 'Embody', 'convoy', 'host')
VENDOR = os.path.join(REPO, VENDOR_REL)


def daemon_modules(src=HERE):
    """Every shipping daemon module, discovered -- never a hardcoded list.

    convoy_install.HOST_MODULES is a hardcoded manifest and has already gone
    stale twice (convoy_hostkeys, then convoy_peers). Globbing is what makes a
    newly added module impossible to forget.
    """
    return sorted(
        f for f in os.listdir(src)
        if f.startswith('convoy_') and f.endswith('.py')
        and not f.startswith('test_')
    )


def _read(path):
    with open(path, 'rb') as fh:
        return fh.read()


def _normalize(data):
    """Compare content, not line-ending representation.

    Embody writes the vendored files with CRLF on Windows while the daemon
    sources are LF, so a raw byte comparison fails on every Windows dev machine
    and passes in CI -- the worst possible test. .gitattributes declares
    `*.py text eol=lf`, so the two copies ARE byte-identical as committed.
    """
    return data.replace(b'\r\n', b'\n')


def survey(src=HERE, vendor=VENDOR):
    """Classify every module. Never writes -- an audit must not alter state."""
    modules = daemon_modules(src)
    out = {'ok': [], 'stale': [], 'missing': [], 'orphaned': []}
    for name in modules:
        dst = os.path.join(vendor, name)
        if not os.path.exists(dst):
            out['missing'].append(name)
        elif _normalize(_read(os.path.join(src, name))) == _normalize(_read(dst)):
            out['ok'].append(name)
        else:
            out['stale'].append(name)
    if os.path.isdir(vendor):
        known = set(modules)
        for f in sorted(os.listdir(vendor)):
            if f.endswith('.py') and f not in known:
                out['orphaned'].append(f)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--check', action='store_true',
                    help='report drift and exit non-zero; write nothing')
    args = ap.parse_args(argv)

    if not os.path.isdir(VENDOR):
        sys.stderr.write('vendor dir does not exist: %s\n' % VENDOR)
        return 2

    s = survey()
    for name in s['ok']:
        print('  ok       %s' % name)
    for name in s['stale']:
        print('  STALE    %s' % name)
    for name in s['missing']:
        print('  MISSING  %s  (needs a text DAT in the host COMP, then externalize)'
              % name)
    for name in s['orphaned']:
        print('  ORPHAN   %s  (vendored but no longer in dev/convoy/)' % name)

    drift = s['stale'] + s['missing'] + s['orphaned']
    if args.check:
        if drift:
            sys.stderr.write(
                '\n%d module(s) drifted. Run without --check to re-vendor.\n'
                % len(drift))
            return 1
        print('\n%d modules vendored and current.' % len(s['ok']))
        return 0

    # MISSING is not fixed by copying: a file with no DAT ships nowhere, so
    # writing it would produce a green --check over a payload that is still
    # incomplete. Say so instead of papering over it.
    for name in s['stale']:
        with open(os.path.join(HERE, name), 'rb') as r:
            data = r.read()
        with open(os.path.join(VENDOR, name), 'wb') as w:
            w.write(data)
        print('  re-vendored  %s' % name)

    if not s['stale']:
        print('\nNothing to do -- all %d modules already current.' % len(s['ok']))
    if s['missing']:
        sys.stderr.write(
            '\n%d module(s) have NO vendored DAT and were NOT written. Create a\n'
            'text DAT of that name in the host COMP and externalize it (tag py),\n'
            'or the module will not ship in the .tox.\n' % len(s['missing']))
        return 1
    if s['orphaned']:
        sys.stderr.write(
            '\n%d vendored file(s) no longer exist in dev/convoy/. Delete the DAT\n'
            '(delete_op) rather than the file, or the next save re-creates it.\n'
            % len(s['orphaned']))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
