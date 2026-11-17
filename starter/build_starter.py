#!/usr/bin/env python3
"""Rebuild Embody-starter.toe from TouchDesigner's own blank project.

The starter is Samples/Setup/Base/NewProject.toe from a TouchDesigner install
plus one Execute DAT, /project1/embody_install, holding embody_install.py.
Run this after editing that script; test_embody_bootstrap fails while the
two differ.

    python starter/build_starter.py [--td <TouchDesigner dir>]

Uses toeexpand / toecollapse through dev/embody/embody_bootstrap.py. The
staging folder is left in the temp directory: nothing here deletes a tree.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STARTER = os.path.join(HERE, 'Embody-starter.toe')
SCRIPT = os.path.join(HERE, 'embody_install.py')
DAT = 'project1/embody_install'

# left of Embody, which loads at (0, 0)
NODE = (b'DAT:execute\ntile -400 0 130 90\nflags =  display on parlanguage 0\n'
        b'color 0.67 0.67 0.67 \nend\n')
PARM = b'?\nstart 0 on\n?\n'


def _bootstrap():
    """dev/embody/embody_bootstrap.py: the .toc and .text format lives there."""
    path = os.path.join(HERE, '..', 'dev', 'embody', 'embody_bootstrap.py')
    spec = importlib.util.spec_from_file_location('embody_bootstrap', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blank_project(install_dir: str) -> str:
    """TouchDesigner's own empty project inside an install (or .app)."""
    hits = glob.glob(os.path.join(install_dir, '**', 'Samples', 'Setup', 'Base',
                                  'NewProject.toe'), recursive=True)
    if not hits:
        raise SystemExit(f'No Samples/Setup/Base/NewProject.toe under {install_dir}')
    return hits[0]


def build(install_dir: str) -> str:
    eb = _bootstrap()
    tools = eb.td_tools(install_dir)
    body = open(SCRIPT, 'rb').read()
    if b'\r' in body or not body.isascii():
        raise SystemExit('embody_install.py must be ASCII with LF line endings')
    staging = tempfile.mkdtemp(prefix='embody_starter_')
    work = os.path.join(staging, 'Embody-starter.toe')
    shutil.copyfile(blank_project(install_dir), work)
    folder, toc = eb.expand(tools['toeexpand'], work)
    files = {'.n': NODE, '.parm': PARM, '.text': eb.encode_text(body)}
    # the blank /project1 has no children, so no folder yet
    os.makedirs(os.path.join(folder, os.path.dirname(DAT)), exist_ok=True)
    for suffix, data in files.items():
        with open(os.path.join(folder, *(DAT + suffix).split('/')), 'wb') as f:
            f.write(data)
    eb.write_toc(toc, eb.read_toc(toc) + [DAT + suffix for suffix in files])
    eb.collapse(tools['toecollapse'], work)
    shutil.copyfile(work, STARTER)
    print(f'Built {STARTER} ({os.path.getsize(STARTER)} bytes); staging: {staging}')
    return STARTER


def main() -> int:
    ap = argparse.ArgumentParser(description='Rebuild Embody-starter.toe.')
    ap.add_argument('--td', help='TouchDesigner install dir (default: the newest found)')
    args = ap.parse_args()
    eb = _bootstrap()
    installs = eb.find_td_installs()
    install_dir = args.td or (installs[0][1] if installs else None)
    if not install_dir:
        raise SystemExit('No TouchDesigner install found; pass --td <install dir>')
    build(install_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
