#!/usr/bin/env python3
"""Verify a pinned Python runtime capsule without accessing Reminders data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "AppleRemindersPythonRuntime.app"
BUNDLE_ID = "io.github.oscar-v4.apple-reminders.python-runtime"
EXECUTABLE = "Contents/MacOS/apple-reminders-python"
ARCHITECTURES = {"arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}
SOURCE_INPUTS = {
    "scripts/build_python_runtime.py",
    "scripts/python_runtime_launcher.c",
    "scripts/python_runtime_info.plist",
    "scripts/python-runtime-lock.json",
    ".github/workflows/prepare-signed-runtime-source.yml",
}
BEHAVIOR_INPUTS = {
    "scripts/python_runtime_launcher.c", "scripts/python_runtime_info.plist",
    "scripts/python-runtime-lock.json",
}
HEX = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 300 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 10_000
MACHO_MAGICS = {bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca"
)}


class VerificationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise VerificationError("runtime input is not a regular non-symlink file")


def relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise VerificationError("invalid runtime inventory path")
    path = PurePosixPath(value)
    if (
        path.is_absolute() or value == "." or str(path) != value or ".." in path.parts
        or "\\" in value or any(ord(char) < 32 or ord(char) > 126 for char in value)
    ):
        raise VerificationError("unsafe runtime inventory path")
    return value


def load_manifest(
    path: Path, architecture: str, *, require_signed: bool = False,
    expected_team_id: str | None = None, repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    regular(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise VerificationError("runtime manifest exceeds bound")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise VerificationError("duplicate runtime manifest key")
            result[key] = value
        return result

    manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(manifest, dict):
        raise VerificationError("runtime manifest must be an object")
    lock = json.loads((repo_root / "scripts/python-runtime-lock.json").read_text())
    expected = {
        "schema_version": 1, "architecture": architecture,
        "target_triple": ARCHITECTURES[architecture],
        "runtime_version": lock["python_version"], "runtime_build": lock["release"],
        "app_name": APP_NAME, "bundle_identifier": BUNDLE_ID,
        "executable_relative_path": EXECUTABLE,
        "upstream": {kind: lock["architectures"][architecture][kind] for kind in ("install_only_stripped", "full")},
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise VerificationError(f"runtime manifest identity drift: {key}")
    source_hashes = manifest.get("source_input_sha256")
    if not isinstance(source_hashes, dict) or set(source_hashes) != SOURCE_INPUTS:
        raise VerificationError("runtime source input inventory drift")
    for name in SOURCE_INPUTS:
        if not isinstance(source_hashes[name], str) or not HEX.fullmatch(source_hashes[name]):
            raise VerificationError("runtime source input digest invalid")
        # Tooling provenance is checked against its historical source commit by
        # the release verifier. Editing a CI workflow does not invalidate an
        # already reviewed interpreter with unchanged behavior inputs.
        source = repo_root / name
        if name in BEHAVIOR_INPUTS:
            regular(source)
        if name in BEHAVIOR_INPUTS and source_hashes[name] != sha256(source):
            raise VerificationError(f"runtime source input hash drift: {name}")
    normalization = manifest.get("normalization")
    if not isinstance(normalization, dict) or set(normalization) != {"omitted_symlinks", "dereferenced_symlinks"}:
        raise VerificationError("runtime normalization inventory drift")
    if normalization["omitted_symlinks"] != ["python/bin/python", "python/bin/python3"]:
        raise VerificationError("runtime interpreter alias normalization drift")
    links = normalization["dereferenced_symlinks"]
    if not isinstance(links, list) or len(links) > 100 or links != sorted(set(links)):
        raise VerificationError("runtime dereferenced link inventory invalid")
    for link in links:
        relative_path(link)
        if not link.startswith("python/"):
            raise VerificationError("runtime normalized link leaves upstream root")
    signed = manifest.get("signature") == "developer-id"
    if require_signed and not signed:
        raise VerificationError("runtime lacks Developer ID provenance")
    if signed:
        if not expected_team_id or not re.fullmatch(r"[A-Z0-9]{10}", expected_team_id):
            raise VerificationError("signed runtime requires an expected Team ID")
        for key, value in {"team_id": expected_team_id, "notarized": True, "notarization_checked": True}.items():
            if manifest.get(key) != value:
                raise VerificationError(f"runtime signature provenance drift: {key}")
        for key in ("source_commit", "workflow_commit"):
            if not isinstance(manifest.get(key), str) or not COMMIT.fullmatch(manifest[key]):
                raise VerificationError(f"runtime commit identity invalid: {key}")
        if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("code_directory_hash", ""))):
            raise VerificationError("runtime signed code identity invalid")
    digest_key, size_key = ("archive_sha256", "archive_bytes") if signed else ("unsigned_archive_sha256", "unsigned_archive_bytes")
    if not HEX.fullmatch(str(manifest.get(digest_key, ""))):
        raise VerificationError("runtime archive digest invalid")
    size = manifest.get(size_key)
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise VerificationError("runtime archive size invalid")
    return manifest


def manifest_inventory(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    files, directories = manifest.get("files"), manifest.get("directories")
    if not isinstance(files, list) or not isinstance(directories, list) or not 1 <= len(files) + len(directories) <= MAX_ENTRIES:
        raise VerificationError("runtime inventory count invalid")
    expected: dict[str, dict[str, Any]] = {APP_NAME + "/": {"mode": 0o755, "directory": True, "bytes": 0}}
    total = 0
    macho_paths = []
    for entry, directory in [(item, False) for item in files] + [(item, True) for item in directories]:
        if not isinstance(entry, dict) or set(entry) != ({"path", "mode"} if directory else {"path", "mode", "bytes", "sha256", "mach_o"}):
            raise VerificationError("runtime entry field inventory drift")
        relative = relative_path(entry["path"])
        name = APP_NAME + "/" + relative + ("/" if directory else "")
        if name in expected or name.rstrip("/") in expected or name + "/" in expected:
            raise VerificationError("duplicate or conflicting runtime entry")
        mode = entry["mode"]
        if type(mode) is not int or mode not in ({0o755} if directory else {0o644, 0o755}):
            raise VerificationError("unsafe runtime file mode")
        if not directory:
            size = entry["bytes"]
            if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES or not HEX.fullmatch(str(entry["sha256"])):
                raise VerificationError("invalid runtime file size or digest")
            if type(entry["mach_o"]) is not bool:
                raise VerificationError("invalid Mach-O classification")
            total += size
            if entry["mach_o"]:
                macho_paths.append(relative)
        expected[name] = {**entry, "directory": directory, "bytes": 0 if directory else entry["bytes"]}
    if total > MAX_TREE_BYTES or manifest.get("macho_paths") != sorted(macho_paths):
        raise VerificationError("runtime tree size or Mach-O inventory drift")
    required = {EXECUTABLE, "Contents/Info.plist", "Contents/Resources/python/bin/python3.13", "Contents/Resources/upstream/PYTHON.json"}
    if not required <= {entry["path"] for entry in files} or not any(entry["path"].startswith("Contents/Resources/upstream/licenses/") for entry in files):
        raise VerificationError("runtime executable, metadata, or licenses missing")
    for executable in (EXECUTABLE, "Contents/Resources/python/bin/python3.13"):
        entry = expected[APP_NAME + "/" + executable]
        if executable not in macho_paths or entry["mode"] != 0o755:
            raise VerificationError("runtime entry point must be an executable Mach-O")
    for name in expected:
        for parent in PurePosixPath(name.rstrip("/")).parents:
            if str(parent) != "." and str(parent) + "/" not in expected:
                raise VerificationError("runtime entry has an undeclared parent")
    return expected


def validate_metadata(
    read: Callable[[str], bytes], manifest: dict[str, Any], expected: dict[str, dict[str, Any]],
) -> None:
    info = plistlib.loads(read(APP_NAME + "/Contents/Info.plist"))
    for key, value in {"CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": "apple-reminders-python", "CFBundlePackageType": "APPL", "LSMinimumSystemVersion": "14.0", "CFBundleShortVersionString": manifest["runtime_version"], "CFBundleVersion": manifest["runtime_build"]}.items():
        if info.get(key) != value:
            raise VerificationError(f"runtime app metadata drift: {key}")
    upstream_root = APP_NAME + "/Contents/Resources/upstream/"
    metadata = json.loads(read(upstream_root + "PYTHON.json"))
    if not isinstance(metadata, dict) or any(metadata.get(key) != manifest[field] for key, field in (("python_version", "runtime_version"), ("target_triple", "target_triple"))):
        raise VerificationError("runtime upstream Python metadata drift")
    target = metadata.get("apple_sdk_deployment_target")
    if not isinstance(target, str) or not re.fullmatch(r"\d+\.\d+", target) or tuple(map(int, target.split("."))) > (14, 0):
        raise VerificationError("runtime upstream deployment target incompatible")
    if metadata.get("license_path") != "licenses/LICENSE.cpython.txt":
        raise VerificationError("runtime CPython license metadata drift")
    license_name = upstream_root + metadata["license_path"]
    if license_name not in expected or not read(license_name).strip():
        raise VerificationError("runtime CPython license is missing or empty")


def validate_archive(archive: Path, manifest: dict[str, Any]) -> None:
    regular(archive)
    signed = manifest.get("signature") == "developer-id"
    size_key = "archive_bytes" if signed else "unsigned_archive_bytes"
    hash_key = "archive_sha256" if signed else "unsigned_archive_sha256"
    if archive.stat().st_size != manifest[size_key] or sha256(archive) != manifest[hash_key]:
        raise VerificationError("runtime archive hash or size mismatch")
    expected = manifest_inventory(manifest)
    with zipfile.ZipFile(archive) as handle:
        entries = handle.infolist()
        if len(entries) != len(expected) or {entry.filename for entry in entries} != set(expected):
            raise VerificationError("runtime ZIP inventory does not match manifest")
        for info in entries:
            entry = expected[info.filename]
            unix = info.external_attr >> 16
            expected_type = stat.S_IFDIR if entry["directory"] else stat.S_IFREG
            if (info.create_system != 3 or stat.S_IFMT(unix) != expected_type
                or stat.S_IMODE(unix) != entry["mode"] or info.flag_bits & 1
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or info.file_size != entry["bytes"]):
                raise VerificationError("runtime ZIP type, mode, compression, or size drift")
            if not entry["directory"]:
                content = handle.read(info)
                if hashlib.sha256(content).hexdigest() != entry["sha256"] or (content[:4] in MACHO_MAGICS) is not entry["mach_o"]:
                    raise VerificationError("runtime ZIP content or code inventory drift")
        validate_metadata(handle.read, manifest, expected)


def validate_existing_app(app: Path, manifest: dict[str, Any]) -> None:
    """Check the exact on-disk inventory without extracting or repairing it."""

    expected = manifest_inventory(manifest)
    app = app.absolute()
    if app.name != APP_NAME or any(path.is_symlink() for path in (app, *app.parents)):
        raise VerificationError("runtime app requires its real non-symlink path")
    root_stat = app.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_IMODE(root_stat.st_mode) != 0o755:
        raise VerificationError("runtime app root type or mode drift")
    seen = {APP_NAME + "/"}
    pending = [app]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for item in entries:
                path = Path(item.path)
                observed = item.stat(follow_symlinks=False)
                is_directory = stat.S_ISDIR(observed.st_mode)
                name = APP_NAME + "/" + path.relative_to(app).as_posix() + ("/" if is_directory else "")
                entry = expected.get(name)
                if entry is None or name in seen:
                    raise VerificationError("runtime app inventory does not match manifest")
                seen.add(name)
                expected_type = stat.S_IFDIR if entry["directory"] else stat.S_IFREG
                if stat.S_IFMT(observed.st_mode) != expected_type or stat.S_IMODE(observed.st_mode) != entry["mode"]:
                    raise VerificationError("runtime app entry type or mode drift")
                if is_directory:
                    pending.append(path)
                else:
                    if observed.st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
                        raise VerificationError("runtime app content hash or size drift")
                    with path.open("rb") as handle:
                        if (handle.read(4) in MACHO_MAGICS) is not entry["mach_o"]:
                            raise VerificationError("runtime app code inventory drift")
    if seen != set(expected):
        raise VerificationError("runtime app inventory does not match manifest")
    validate_metadata(lambda name: (app.parent / name).read_bytes(), manifest, expected)


def extract_verified(archive: Path, manifest: dict[str, Any], destination: Path) -> Path:
    validate_archive(archive, manifest)
    if destination.is_symlink() or destination.exists():
        raise VerificationError("runtime extraction requires a new destination")
    for parent in destination.parents:
        if parent.is_symlink():
            raise VerificationError("runtime extraction parent is a symlink")
    destination.mkdir(parents=True, mode=0o700)
    with zipfile.ZipFile(archive) as handle:
        for info in sorted(handle.infolist(), key=lambda item: (len(PurePosixPath(item.filename).parts), item.filename)):
            path = destination / info.filename
            if info.is_dir():
                path.mkdir(mode=0o755)
            else:
                with path.open("xb") as output, handle.open(info) as source:
                    while data := source.read(1024 * 1024):
                        output.write(data)
                path.chmod(stat.S_IMODE(info.external_attr >> 16))
    return destination / APP_NAME


def run(argv: list[str], *, timeout: int = 90, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    if result.returncode:
        raise VerificationError(f"runtime verification command failed: {Path(argv[0]).name} ({result.returncode})")
    return result.stdout


def verify_signatures(app: Path, architecture: str, team_id: str, *, notarized: bool, macho_paths: list[str], expected_cdhash: str | None = None) -> None:
    developer_id = 'certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
    requirement = f'anchor apple generic and identifier "{BUNDLE_ID}" and certificate leaf[subject.OU] = "{team_id}" and {developer_id}'
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", f"={requirement}", str(app)])
    if expected_cdhash is not None:
        result = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(app)], capture_output=True, text=True, timeout=30, check=False)
        matches = re.findall(r"^CDHash=([0-9a-f]{40})$", result.stderr, re.MULTILINE)
        if result.returncode or matches != [expected_cdhash]:
            raise VerificationError("runtime signed code identity does not match manifest")
    nested_requirement = f'anchor apple generic and certificate leaf[subject.OU] = "{team_id}" and {developer_id}'
    for relative in macho_paths:
        path = app / relative
        run(["/usr/bin/codesign", "--verify", "--strict", "-R", f"={nested_requirement}", str(path)])
        if run(["/usr/bin/lipo", "-archs", str(path)]).strip().split() != [architecture]:
            raise VerificationError("runtime Mach-O architecture mismatch")
    if notarized:
        run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])
        run(["/usr/bin/xcrun", "stapler", "validate", str(app)])


def run_probes(app: Path, architecture: str, plugin_root: Path | None) -> None:
    executable = app / EXECUTABLE
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON") and key != "__PYVENV_LAUNCHER__"}
    env.update({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONHOME": "/missing-python", "PYTHONPATH": "/missing-site", "__PYVENV_LAUNCHER__": "/missing-launcher"})
    probe = """import bz2,ctypes,hashlib,json,lzma,os,platform,sqlite3,ssl,subprocess,sys,tomllib,zoneinfo
callback=ctypes.CFUNCTYPE(ctypes.c_int,ctypes.c_int)(lambda value: value+1)
assert callback(3)==4
assert sys.version_info[:3]==(3,13,15)
assert sys.flags.no_user_site and sys.dont_write_bytecode
assert zoneinfo.ZoneInfo('Asia/Seoul').key=='Asia/Seoul'
assert os.path.realpath(sys.executable).endswith('/Contents/Resources/python/bin/python3.13')
child=subprocess.check_output([sys.executable,'-c','import sqlite3,ssl,sys;print(sys.version_info[:3])'],text=True)
assert '(3, 13, 15)' in child
print(json.dumps({'architecture':platform.machine(),'python':platform.python_version()}))
"""
    result = json.loads(run([str(executable), "-c", probe], env=env))
    if result.get("architecture") != architecture:
        raise VerificationError("runtime native architecture probe mismatch")
    if plugin_root is not None:
        wire = '\n'.join(json.dumps(item) for item in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "runtime-verifier", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )) + '\n'
        for experimental in (False, True):
            argv = [str(executable), str(plugin_root.resolve() / "mcp/server.py"), *(["--experimental"] if experimental else [])]
            result = subprocess.run(argv, input=wire, capture_output=True, text=True, env=env, timeout=30, check=False)
            if result.returncode != 0:
                raise VerificationError("bundled runtime MCP discovery failed")
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            if len(responses) != 2 or len(responses[1]["result"]["tools"]) != (15 if experimental else 9):
                raise VerificationError("bundled runtime MCP tool profile drift")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--app", type=Path, help="Verify an existing app in place without extracting, repairing, or executing it")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--architecture", choices=sorted(ARCHITECTURES), required=True)
    parser.add_argument("--expected-team-id")
    parser.add_argument("--require-developer-id", action="store_true")
    parser.add_argument("--require-notarized", action="store_true")
    parser.add_argument("--run-probes", action="store_true")
    parser.add_argument("--plugin-root", type=Path)
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args(argv)
    if args.app and (args.extract_to or args.run_probes):
        parser.error("--app cannot be combined with --extract-to or --run-probes")
    try:
        manifest = load_manifest(args.manifest, args.architecture, require_signed=args.require_developer_id or args.require_notarized, expected_team_id=args.expected_team_id)

        def check_app(app: Path) -> None:
            if args.require_developer_id or args.require_notarized:
                verify_signatures(app, args.architecture, args.expected_team_id, notarized=args.require_notarized, macho_paths=manifest["macho_paths"], expected_cdhash=manifest["code_directory_hash"])
            if args.run_probes:
                run_probes(app, args.architecture, args.plugin_root)

        if args.app:
            validate_existing_app(args.app, manifest)
            check_app(args.app)
        else:
            validate_archive(args.archive, manifest)
            with tempfile.TemporaryDirectory(prefix="apple-reminders-runtime-verify-") as temporary:
                if args.require_developer_id or args.require_notarized or args.run_probes or args.extract_to:
                    destination = args.extract_to or Path(temporary).resolve() / "extracted"
                    check_app(extract_verified(args.archive, manifest, destination))
        print(json.dumps({"ok": True, "architecture": args.architecture, "runtime_version": manifest["runtime_version"], "file_count": len(manifest["files"]), "archive_bytes": args.archive.stat().st_size if args.archive else None, "existing_app_checked": args.app is not None, "signature_checked": args.require_developer_id or args.require_notarized, "notarization_checked": args.require_notarized, "probes_run": args.run_probes}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError, VerificationError) as exc:
        parser.exit(1, f"Python runtime verification failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
