# me - this DAT
# 
# frame - the current frame
# state - True if the timeline is paused
# 
# Make sure the corresponding toggle is enabled in the Execute DAT.

from pathlib import Path

comp = op.Embody
readme = Path(project.folder).parents[0] / 'README.md'

def version(version):
    # get current versions
    increment = int(version.rsplit('.', 1)[1])
    major_minor = version.rsplit('.', 1)[0]
    # update version
    increment += 1
    new_version = f"{major_minor}.{increment}"
    comp.par.Version.val = new_version
    return new_version

def updateReadme(build, version):
    # load the file into file_content
    with open(readme, 'r') as f:
        file_content = f.readlines()

    # Overwrite it
    with open(readme, 'w') as writer:
        for line in file_content:
            # We search for the correct section
            build_pre = '#### :floppy_disk: TouchDesigner'
            version_pre = '#### :floppy_disk: version'

            if line.startswith(build_pre):
                line = f"{build_pre} {build} (Windows/macOS)\n"
            elif line.startswith(version_pre):
                line = f"{version_pre} {version}\n"

            # Re-write the file at each iteration
            writer.write(line)

def onStart():
    return

def onCreate():
    return

def onExit():
    return

def onFrameStart(frame):
    return

def onFrameEnd(frame):
    return

def onPlayStateChange(state):
    return

def onDeviceChange():
    return

def onProjectPreSave():
    # set page for component
    comp.currentPage = 'Embody'

    build = project.saveBuild
    old_version = comp.par.Version.val
    new_version = version(old_version)

    # version up (dev)
    updateReadme(build, new_version)

    # update build
    comp.par.Touchbuild = build

    # try to delete last release
    try:
        old_release = Path(project.folder).parents[0] / 'release' / f"{comp.name}-v{old_version}.tox"
        old_release.unlink()
    except Exception as e:
        # You might want to log the exception for debugging purposes
        # print(f"Error deleting old release: {e}")
        pass

    # save out self-contained portable .tox (strips external file references)
    save_path = Path(project.folder).parents[0] / 'release' / f"{comp.name}-v{new_version}.tox"
    comp.ExportPortableTox(save_path=str(save_path))

def onProjectPostSave():
    return
