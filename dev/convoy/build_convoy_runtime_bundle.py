"""Build an OFFLINE Convoy managed-runtime release archive.

This tool never downloads Python, wheels, or build tools. Release CI must hand
it an already prepared, platform-native, self-contained CPython tree whose
`cryptography` install and OS signing/notarization have been completed. The
tool live-probes that exact interpreter, inventories every ordinary file,
writes a deterministic ZIP, and emits the SHA-256 metadata consumed by
`convoy_install.provision_runtime_bundle`. Its metadata is deliberately a
catalog-ineligible candidate until the platform release job verifies signing
and promotes the matching record into `convoy_runtime_catalog.json`.

Run this script on the target OS/architecture. It intentionally cannot build a
Windows archive on macOS or an Apple Silicon archive on Windows.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import sys
import zipfile


HERE = pathlib.Path(__file__).resolve().parent
INSTALLER_PATH = (HERE.parent / "embody" / "Embody" / "convoy"
                  / "convoy_install.py")
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "convoy_runtime_bundle_installer", str(INSTALLER_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _ordinary_files(root):
    root = pathlib.Path(root).resolve()
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("runtime roots may not contain symlinks: %s" % path)
        if path.is_dir():
            continue
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("runtime roots may contain ordinary files only: %s"
                             % path)
        relative = path.relative_to(root).as_posix()
        found.append((relative, path, stat.S_IMODE(mode)))
    return found


def _copy_verified(source_path, destination, expected_size, expected_sha256):
    """Copy a file while proving it did not change after inventory."""
    digest = hashlib.sha256()
    size = 0
    with open(source_path, "rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            destination.write(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise ValueError("runtime source changed while bundle was being built: %s"
                         % source_path)


def build_bundle(runtime_root, output, runtime_id, python_relative,
                 probe_python_relative, platform_name, architecture,
                 source_revision,
                 installer=None, probe_runner=None):
    """Build and return the detached trusted-release metadata object."""
    installer = installer or _load_installer()
    runtime_root = pathlib.Path(runtime_root).resolve()
    output = pathlib.Path(output).resolve()
    try:
        output.relative_to(runtime_root)
    except ValueError:
        pass
    else:
        raise ValueError("bundle output must be outside the runtime root")
    architecture = installer.normalize_architecture(architecture)
    if sys.platform != platform_name:
        raise ValueError("runtime bundles must be built on their target OS")
    if installer.normalize_architecture() != architecture:
        raise ValueError("runtime bundles must be built on their target architecture")
    if not installer.runtime_target_supported(platform_name, architecture):
        raise ValueError("unsupported Convoy runtime target %s/%s"
                         % (platform_name, architecture))
    runtime_id = installer.safe_version(runtime_id)
    source_revision = str(source_revision or "").strip()
    if not source_revision:
        raise ValueError("source-revision is required for release provenance")
    python_relative = installer._safe_runtime_relpath(python_relative)
    probe_python_relative = installer._safe_runtime_relpath(
        probe_python_relative)
    if python_relative is None:
        raise ValueError("python-relative is not a safe bundle path")
    if probe_python_relative is None:
        raise ValueError("probe-python-relative is not a safe bundle path")
    python_path = runtime_root.joinpath(*python_relative.split("/"))
    probe_python_path = runtime_root.joinpath(
        *probe_python_relative.split("/"))
    if not python_path.is_file():
        raise ValueError("managed runtime Python does not exist: %s" % python_path)
    if not probe_python_path.is_file():
        raise ValueError("runtime probe Python does not exist: %s"
                         % probe_python_path)
    if platform_name == "darwin":
        if not os.access(python_path, os.X_OK):
            raise ValueError("managed runtime Python is not executable")
        if not os.access(probe_python_path, os.X_OK):
            raise ValueError("runtime probe Python is not executable")

    probe = installer.probe_runtime(
        str(probe_python_path), platform_name, architecture,
        runner=probe_runner)
    if not probe.get("ok"):
        raise ValueError("runtime capability probe failed: %s"
                         % (probe.get("detail") or probe.get("reason")))
    self_contained, outside_field = installer._probe_paths_inside_runtime(
        probe["probe"], str(runtime_root))
    if not self_contained:
        raise ValueError("%s loaded outside the prepared runtime root"
                         % outside_field)

    files = []
    source_files = _ordinary_files(runtime_root)
    for relative, path, mode in source_files:
        if relative in (installer.COMPLETE_FILE,
                        installer.RUNTIME_MANIFEST_FILE):
            raise ValueError("runtime root contains an installer control file")
        files.append({
            "path": relative,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "mode": mode,
        })
    manifest = {
        "format": installer.RUNTIME_BUNDLE_FORMAT,
        "runtime_id": runtime_id,
        "platform": platform_name,
        "architecture": architecture,
        "python": python_relative,
        "probe_python": probe_python_relative,
        "python_version": ".".join(str(v) for v in
                                   probe["probe"]["python"]),
        "cryptography_version": probe["probe"]["cryptography_version"],
        "source_revision": str(source_revision),
        "files": files,
    }
    checked = installer._validate_runtime_manifest(
        manifest, platform_name, architecture)
    if not checked.get("ok"):
        raise ValueError(checked.get("detail") or checked.get("reason"))
    manifest = checked["manifest"]

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp-%s" % os.getpid())
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            manifest_info = zipfile.ZipInfo(installer.RUNTIME_MANIFEST_FILE,
                                            ZIP_TIMESTAMP)
            manifest_info.compress_type = zipfile.ZIP_DEFLATED
            manifest_info.external_attr = (0o644 | stat.S_IFREG) << 16
            archive.writestr(
                manifest_info,
                (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode(
                    "utf-8"))
            by_name = {relative: (path, mode)
                       for relative, path, mode in source_files}
            for item in manifest["files"]:
                path, mode = by_name[item["path"]]
                info = zipfile.ZipInfo(item["path"], ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (mode | stat.S_IFREG) << 16
                with archive.open(info, "w") as dest:
                    _copy_verified(path, dest, item["size"], item["sha256"])
        os.replace(temp, output)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass

    return {
        "format": installer.RUNTIME_RELEASE_FORMAT,
        "runtime_id": runtime_id,
        "platform": platform_name,
        "architecture": architecture,
        "asset": output.name,
        "size": output.stat().st_size,
        "sha256": _sha256(output),
        "python_version": manifest["python_version"],
        "cryptography_version": manifest["cryptography_version"],
        "source_revision": source_revision,
        # Deliberately catalog-ineligible. The platform release job must verify
        # Authenticode or Developer ID + notarization AFTER the exact archive
        # bytes are final, then change these fields when publishing catalog
        # metadata. The builder cannot honestly attest an external signature.
        "status": "candidate",
        "current": False,
        "signature": "verification-required",
    }


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--python-relative", required=True)
    parser.add_argument("--probe-python-relative", required=True)
    parser.add_argument("--platform", required=True, choices=("win32", "darwin"))
    parser.add_argument("--architecture", required=True,
                        choices=("x86_64", "arm64"))
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--metadata-output")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    metadata = build_bundle(
        args.runtime_root, args.output, args.runtime_id,
        args.python_relative, args.probe_python_relative, args.platform,
        args.architecture, args.source_revision)
    target = pathlib.Path(args.metadata_output or
                          (str(args.output) + ".release.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp-%s" % os.getpid())
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(metadata, indent=1, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    sys.stdout.write(json.dumps(metadata, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
