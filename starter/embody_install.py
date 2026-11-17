"""Embody starter: installs the latest Embody release when this project opens.

Downloads the release .tox named by the release manifest, checks its sha256
and loads it beside this DAT: the Quickstart's Textport paste, run for you.
With no connection it asks for a release .tox already on this machine.
Removes itself once Embody is in the project.

The download runs on a Thread Manager worker, which touches no TouchDesigner
object; loading, the file prompt and the self-removal happen on the main
thread.

build_starter.py bakes this file into Embody-starter.toe; rebuild after any
edit here.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Callable, Optional, Tuple

MANIFEST = ('https://github.com/dylanroscover/Embody/releases/latest/'
            'download/embody-release.json')
ASSET = 'https://github.com/dylanroscover/Embody/releases/download/%s/%s'
HEADERS = {'User-Agent': 'Embody-Install'}
# (connect, read) seconds: a dead network reaches the file prompt quickly
TIMEOUT = (5, 60)


def onStart() -> None:
    # a few frames in, so the window has drawn first
    run('args[0]()', install, delayFrames=30)


def download(get: Callable) -> Tuple[str, str]:
    """Fetch the latest release .tox, sha256-checked: (path, version)."""
    manifest = get(MANIFEST, headers=HEADERS, timeout=TIMEOUT).json()
    data = get(ASSET % (manifest['tag'], manifest['asset']), headers=HEADERS,
               timeout=TIMEOUT).content
    if hashlib.sha256(data).hexdigest() != manifest['sha256']:
        raise ValueError('checksum mismatch')
    path = os.path.join(tempfile.gettempdir(),
                        os.path.basename(manifest['asset']))
    with open(path, 'wb') as f:
        f.write(data)
    return path, str(manifest['version'])


def install(get: Optional[Callable] = None) -> None:
    """Fetch the latest release on a worker, unless Embody is already here."""
    if root.findChildren(name='Embody', parName='Toxtag', maxDepth=2):
        me.destroy()  # embody_bootstrap grafted it in before the open
        return
    found = {}

    def worker() -> None:
        # worker thread: plain Python only, no TouchDesigner object
        fetch = get
        if fetch is None:
            import requests
            fetch = requests.get
        found['release'] = download(fetch)

    # the hooks run on the main thread; a frame later, clear of the manager
    def done() -> None:
        run('args[0](*args[1])', load, found['release'], delayFrames=1)

    def failed(error: object) -> None:
        run('args[0](args[1])', ask, error, delayFrames=1)

    manager = op.TDResources.ThreadManager
    task = manager.TDTask(target=worker, SuccessHook=done, ExceptHook=failed)
    if manager.EnqueueTask(task, standalone=True) is None:
        ask('no worker thread free')


def ask(error: object) -> None:
    """The download failed: offer a release .tox already on this machine."""
    # the Thread Manager hands its ExceptHook an exc_info tuple
    reason = error[1] if isinstance(error, tuple) and len(error) > 1 else error
    print('Embody starter: download failed (%s)' % (reason,))
    path = ui.chooseFile(
        load=True, fileTypes=['tox'],
        title='Embody could not be downloaded: select a release .tox')
    if not path:
        print('Embody starter: nothing installed. Reopen this project to '
              'try again.')
        return
    load(path, os.path.basename(path))


def load(path: str, version: str) -> None:
    """Load the release beside this DAT, then retire the DAT."""
    embody = me.parent().loadTox(path)
    print('Embody', version, 'installed at', embody.path)
    me.destroy()
