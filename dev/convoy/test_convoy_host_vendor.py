"""Vendor parity: the host-app DATs must BE the daemon sources.

The shipped .tox cannot reach dev/convoy/ -- that path exists only in
this checkout -- so every production host-app module is vendored a
second time as a text DAT under the `host` baseCOMP inside the Embody
COMP, externalized to dev/embody/Embody/convoy/host/convoy_*.py. That is
the deliberate two-copy tax section 1.1 of CONVOY_INSTALL_PLAN.md takes
on, and THIS FILE is the thing that was promised in exchange for taking
it. Without it, an edit to the daemon ships a stale payload to every
user and nothing anywhere says so.

WHY THIS DISCOVERS THE MODULE LIST INSTEAD OF READING HOST_MODULES.
convoy_install.HOST_MODULES is a hand-maintained tuple, and its own
docstring says it is "a manifest of what to vendor, not a gate". A
parity test that iterated it could only ever check the modules someone
had already remembered -- so the single failure it exists to catch, a
NEW daemon module that nobody added to either list, is precisely the one
it would sail past. It has happened: the tuple went out missing a module
an in-flight slice had added. So the daemon sources on disk are the
authority here, discovered by glob, and the tuple is checked only in the
direction that cannot rot the other way (see the last test).

LINE ENDINGS ARE NORMALISED, AND THAT IS NOT A FUDGE. .gitattributes
declares `*.py text eol=lf`, so both copies are byte-identical AS
COMMITTED and CI compares like for like. The CRLF exists only in a
Windows WORKING TREE, because Embody writes every externalized DAT with
the platform's newline -- shared machinery, far outside this feature's
scope. Measured 2026-08-01: all ten modules were raw_bytes_equal=False
and newline_normalized_equal=True, the delta being exactly one \\r per
line (convoy_hostapp.py: 168290 source vs 171509 vendored, delta 3219).
A raw-byte assertion would therefore FAIL ON EVERY WINDOWS DEV MACHINE
while passing in CI -- the worst possible test, one that is loudest
where it is least useful and silent where it matters. Everything except
the newline is still compared byte for byte, so an encoding change, a
BOM, or a stray lone \\r all still fail.
"""

import os
import re

import pytest

# dev/convoy/ -> dev/ -> the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DAEMON_DIR = os.path.join(_REPO_ROOT, 'dev', 'convoy')
HOST_DIR = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody', 'convoy',
                        'host')

# The vendored tree is created by the vendoring step (plan STEP 3). Until
# it lands there is nothing to compare, and ERRORING on that would make
# an unrelated branch red. A skip is the honest reading -- but ONLY for
# the directory being absent: an EMPTY or PARTIAL host/ is real drift and
# must fail.
pytestmark = pytest.mark.skipif(
    not os.path.isdir(HOST_DIR),
    reason='the vendored host modules (%s) do not exist yet' % (HOST_DIR,))


def _python_modules(directory):
    """Every production convoy_*.py in `directory`, by bare filename.

    `convoy_*.py` already excludes the suite (test_convoy_*.py does not
    start with convoy_) and the manual probes (manual_exit_proof.py), but
    the test_ filter is spelled out anyway: the day someone adds
    dev/convoy/convoy_test_helpers.py, silently vendoring a test helper
    into every user's install is not the outcome anybody wants to debug.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [n for n in names
            if n.startswith('convoy_') and n.endswith('.py')
            and not n.startswith('test_')]


def _normalised(path):
    """File bytes with CRLF collapsed to LF. See the module docstring."""
    with open(path, 'rb') as f:
        return f.read().replace(b'\r\n', b'\n')


def _first_difference(left, right):
    """A short, readable report of where two byte blobs diverge."""
    left_lines = left.split(b'\n')
    right_lines = right.split(b'\n')
    for number, (a, b) in enumerate(zip(left_lines, right_lines), start=1):
        if a != b:
            return ('line %d differs:\n  daemon:   %r\n  vendored: %r'
                    % (number, a[:120], b[:120]))
    return ('the files agree for %d lines but differ in length '
            '(daemon %d lines / %d bytes, vendored %d lines / %d bytes)'
            % (min(len(left_lines), len(right_lines)),
               len(left_lines), len(left), len(right_lines), len(right)))


DAEMON_MODULES = _python_modules(DAEMON_DIR)


def test_convoy_tdn_carries_one_text_dat_asset_for_every_daemon_module():
    """Parity on disk is insufficient if the .tox has no DAT to carry it.

    ConvoyExt reads children of ``convoy/host`` at install time.  The TDN is
    the version-controlled network source, so its file parameters must name
    every vendored module exactly once.  This catches the easy-to-miss state
    where a developer copies a file into ``host/`` but never creates the
    corresponding TouchDesigner text DAT.
    """
    tdn = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody', 'convoy.tdn')
    with open(tdn, 'r', encoding='utf-8') as f:
        text = f.read()
    refs = re.findall(
        r'^\s+file:\s+embody/Embody/convoy/host/(convoy_[A-Za-z0-9_]+\.py)\s*$',
        text, flags=re.MULTILINE)
    duplicates = sorted(name for name in set(refs) if refs.count(name) != 1)
    assert not duplicates, 'host module DAT references are duplicated: %r' % duplicates
    assert set(refs) == set(DAEMON_MODULES), (
        'convoy.tdn host DAT assets differ from daemon modules; missing=%r, '
        'extra=%r' % (sorted(set(DAEMON_MODULES) - set(refs)),
                      sorted(set(refs) - set(DAEMON_MODULES))))


def test_the_daemon_source_glob_actually_found_something():
    """A vacuous parity suite is worse than no parity suite.

    Every assertion below is parametrised on DAEMON_MODULES, so a glob
    that quietly matched nothing (a moved directory, a renamed
    convention) would collect zero cases and report a clean green run
    over an entirely unchecked vendoring step.
    """
    assert len(DAEMON_MODULES) >= 5, (
        'expected the Convoy host app daemon sources in %s, found %r'
        % (DAEMON_DIR, DAEMON_MODULES))


@pytest.mark.parametrize('name', DAEMON_MODULES)
def test_every_daemon_module_is_vendored_and_identical(name):
    """The forward direction: nothing ships stale, nothing ships missing.

    A missing counterpart is the sharper of the two failures. The daemon
    imports its modules by name at startup, so a module added to
    dev/convoy/ and never vendored produces a payload that cannot start
    at all -- and it fails at LOGIN, on a user's machine, an hour after
    the release, with no TouchDesigner open to notice.
    """
    vendored = os.path.join(HOST_DIR, name)
    assert os.path.isfile(vendored), (
        '%s has no vendored counterpart at %s -- the shipped .tox would '
        'install a payload missing this module. Vendor it as a text DAT '
        'in the `host` baseCOMP inside the convoy COMP.' % (name, vendored))
    source = _normalised(os.path.join(DAEMON_DIR, name))
    shipped = _normalised(vendored)
    assert source == shipped, (
        'the vendored copy of %s has drifted from dev/convoy/%s -- '
        're-vendor it; %s' % (name, name, _first_difference(source, shipped)))


def test_no_vendored_file_without_a_daemon_source():
    """The reverse direction: a copy nobody maintains any more.

    A vendored module whose daemon source was deleted or renamed is not
    harmless. Nothing updates it again, so it ships forever at the
    version it was abandoned at -- and because the payload directory is
    on the daemon's import path, a stale module can SHADOW the real one
    if the rename went the other way.
    """
    vendored = set(_python_modules(HOST_DIR))
    orphans = sorted(vendored - set(DAEMON_MODULES))
    assert not orphans, (
        'vendored under %s with no source in dev/convoy/: %r -- delete the '
        'DAT, or restore the daemon module it was copied from'
        % (HOST_DIR, orphans))


def test_host_modules_never_names_a_module_that_does_not_exist():
    """The ONE direction HOST_MODULES is safe to assert in.

    Subset only, deliberately. The tuple is a manifest, not a gate --
    asserting equality would turn every new daemon module into a failure
    in convoy_install.py rather than here, which is the wrong place to
    learn about it and the reason this file discovers its own list. But a
    tuple naming a module that no longer exists is pure rot with no
    upside, and the parity tests above cannot see it.
    """
    import importlib.util

    path = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody', 'convoy',
                        'convoy_install.py')
    spec = importlib.util.spec_from_file_location('convoy_install_vendorchk',
                                                  path)
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)

    missing = sorted(set(install.HOST_MODULES) - set(DAEMON_MODULES))
    assert not missing, (
        'convoy_install.HOST_MODULES names %r, which no longer exists in '
        '%s' % (missing, DAEMON_DIR))
