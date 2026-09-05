#!/usr/bin/env python3
"""Assemble a pinned, unsigned macOS Python runtime without executing it.

Only build hosts need Python and the Apple command-line toolchain. Distribution
signing and notarization happen in a separate workflow with no repository code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "AppleRemindersPythonRuntime.app"
BUNDLE_IDENTIFIER = "io.github.oscar-v4.apple-reminders.python-runtime"
EXECUTABLE_RELATIVE_PATH = "Contents/MacOS/apple-reminders-python"
MINIMUM_MACOS = "14.0"
LOCK_PATH = REPO_ROOT / "scripts/python-runtime-lock.json"
SOURCE_PATHS = (
    "scripts/build_python_runtime.py",
    "scripts/python_runtime_launcher.c",
    "scripts/python_runtime_info.plist",
    "scripts/python-runtime-lock.json",
)
WORKFLOW_PATH = ".github/workflows/prepare-signed-runtime-source.yml"
MAX_FILES = 10000
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TREE_BYTES = 300 * 1024 * 1024
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
OMITTED_SYMLINKS = ("python/bin/python", "python/bin/python3")


class BuildFailure(RuntimeError):
    """Reject invalid inputs or incomplete build artifacts."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(path: Path, *, regular: bool = False) -> Path:
    """Reject symlink components, apart from macOS's fixed system aliases."""
    absolute = Path(os.path.abspath(path.expanduser()))
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            # /tmp is a system alias on macOS, not a caller-selected redirect.
            allowed = {"/tmp": "/private/tmp", "/var": "/private/var", "/etc": "/private/etc"}
            if platform.system() != "Darwin" or str(component) not in allowed or str(component.resolve()) != allowed[str(component)]:
                raise BuildFailure(f"symlinked path is not allowed: {component}")
    result = absolute.resolve()
    if regular and (not result.is_file() or not stat.S_ISREG(result.stat().st_mode)):
        raise BuildFailure(f"input is not a regular file: {result}")
    return result


def run(argv: list[str], *, limit: int = 4 * 1024 * 1024) -> bytes:
    """Build utilities only; no assembled runtime is ever launched here."""
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=180, check=False)
    if result.returncode:
        raise BuildFailure(f"build command failed ({Path(argv[0]).name}): {result.stderr[-4000:].decode('utf-8', 'replace')}")
    if len(result.stdout) > limit:
        raise BuildFailure("build command output exceeds its bound")
    return result.stdout


def load_lock(path: Path = LOCK_PATH) -> dict[str, Any]:
    lock = json.loads(safe_path(path, regular=True).read_text(encoding="utf-8"))
    if (lock.get("schema_version"), lock.get("provider"), lock.get("release"), lock.get("python_version"), lock.get("python_major_minor")) != (
        1, "astral-sh/python-build-standalone", "20260901", "3.13.15", "3.13"
    ):
        raise BuildFailure("unreviewed Python runtime lock identity")
    if set(lock.get("architectures", {})) != {"arm64", "x86_64"}:
        raise BuildFailure("runtime lock must contain both Mac architectures")
    for arch, config in lock["architectures"].items():
        triple = ("aarch64" if arch == "arm64" else "x86_64") + "-apple-darwin"
        if config.get("target_triple") != triple:
            raise BuildFailure("runtime lock architecture mismatch")
        for kind, suffix in (("install_only_stripped", "install_only_stripped.tar.gz"), ("full", "pgo+lto-full.tar.zst")):
            pin = config[kind]
            name = f"cpython-{lock['python_version']}+{lock['release']}-{triple}-{suffix}"
            expected_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/" + name.replace("+", "%2B")
            if pin.get("name") != name or pin.get("url") != expected_url or not re.fullmatch(r"[0-9a-f]{64}", pin.get("sha256", "")) or type(pin.get("bytes")) is not int or not 0 < pin["bytes"] < 100 * 1024 * 1024:
                raise BuildFailure("invalid pinned upstream asset")
    return lock


def verified_archive(pin: dict[str, Any], supplied: Path | None, cache: Path) -> Path:
    if supplied is not None:
        archive = safe_path(supplied, regular=True)
    else:
        cache = safe_path(cache)
        cache.mkdir(parents=True, exist_ok=True)
        archive = safe_path(cache / pin["name"])
        if not archive.exists():
            descriptor, name = tempfile.mkstemp(prefix=".python-download-", dir=cache)
            temporary = Path(name)
            try:
                count = 0
                with os.fdopen(descriptor, "wb") as destination, urllib.request.urlopen(pin["url"], timeout=60) as response:
                    while chunk := response.read(1024 * 1024):
                        count += len(chunk)
                        if count > pin["bytes"]:
                            raise BuildFailure("download exceeds pinned asset size")
                        destination.write(chunk)
                if count != pin["bytes"] or sha256(temporary) != pin["sha256"]:
                    raise BuildFailure("download does not match pinned asset")
                if archive.exists():
                    raise BuildFailure("archive destination appeared during download")
                os.replace(temporary, archive)
            finally:
                temporary.unlink(missing_ok=True)
    archive = safe_path(archive, regular=True)
    if archive.stat().st_size != pin["bytes"] or sha256(archive) != pin["sha256"]:
        raise BuildFailure(f"upstream archive checksum/size mismatch: {archive.name}")
    return archive


def member_path(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if not re.fullmatch(r"[A-Za-z0-9_.+@%/-]+", name) or value.is_absolute() or ".." in value.parts or "//" in name or name.startswith("./") or str(value) != name.rstrip("/"):
        raise BuildFailure(f"unsafe archive member: {name!r}")
    if not value.parts or value.parts[0] != "python":
        raise BuildFailure("archive member is outside the Python tree")
    return value


def extract_install(archive: Path, destination: Path) -> dict[str, list[str]]:
    """Copy regular members and resolve safe file symlinks without creating links."""
    destination = safe_path(destination)
    if destination.exists():
        raise BuildFailure("runtime extraction destination must not exist")
    with tarfile.open(safe_path(archive, regular=True), "r:gz") as source:
        members: dict[str, tarfile.TarInfo] = {}
        total = 0
        for member in source.getmembers():
            name = str(member_path(member.name))
            if name in members:
                raise BuildFailure("duplicate archive member")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise BuildFailure("archive contains a hard link or special file")
            if member.size < 0 or member.size > MAX_FILE_BYTES:
                raise BuildFailure("archive member exceeds its size bound")
            total += member.size
            members[name] = member
            if len(members) > MAX_FILES or total > MAX_TREE_BYTES:
                raise BuildFailure("archive exceeds runtime bounds")

        def regular_target(name: str) -> tarfile.TarInfo:
            seen: set[str] = set()
            while True:
                if name in seen or len(seen) >= 32 or name not in members:
                    raise BuildFailure("unresolved or cyclic archive symlink")
                seen.add(name)
                target = members[name]
                if target.isfile():
                    return target
                if not target.issym() or target.linkname.startswith("/") or "\\" in target.linkname:
                    raise BuildFailure("archive symlink must resolve to an in-tree regular file")
                name = posixpath.normpath(posixpath.join(posixpath.dirname(name), target.linkname))
                member_path(name)

        # Validate parent types and all links before creating anything.
        resolved: dict[str, tarfile.TarInfo] = {}
        for name, member in members.items():
            for parent in PurePosixPath(name).parents:
                if str(parent) in members and not members[str(parent)].isdir():
                    raise BuildFailure("archive file or symlink used as a parent directory")
            if not member.isdir():
                resolved[name] = regular_target(name)
        if sum(item.size for item in resolved.values()) > MAX_TREE_BYTES:
            raise BuildFailure("dereferenced runtime exceeds its size bound")
        normalization: dict[str, list[str]] = {"omitted_symlinks": [], "dereferenced_symlinks": []}
        for name in OMITTED_SYMLINKS:
            if name in members and not members[name].issym():
                raise BuildFailure("reviewed redundant interpreter aliases must be symlinks")
        destination.mkdir(parents=True, mode=0o755)
        for name, member in sorted(members.items()):
            if name in OMITTED_SYMLINKS:
                normalization["omitted_symlinks"].append(name)
                continue
            if member.issym():
                normalization["dereferenced_symlinks"].append(name)
            relative = PurePosixPath(name).relative_to("python")
            output = destination.joinpath(*relative.parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            target = resolved[name]
            stream = source.extractfile(target)
            if stream is None:
                raise BuildFailure("archive regular member has no content")
            with stream, output.open("xb") as handle:
                shutil.copyfileobj(stream, handle, 1024 * 1024)
            if output.stat().st_size != target.size:
                raise BuildFailure("archive regular member size changed")
            output.chmod(0o755 if target.mode & 0o111 else 0o644)
        return normalization


def extract_provenance(archive: Path, destination: Path, lock: dict[str, Any], architecture: str) -> None:
    """Read selected full-archive members to stdout; never extract its build tree."""
    names = run(["/usr/bin/tar", "-tf", str(archive)]).decode("utf-8").splitlines()
    if len(names) > 30000 or len(names) != len(set(names)):
        raise BuildFailure("invalid full archive inventory")
    selected = [name for name in names if re.fullmatch(r"python/licenses/LICENSE[.A-Za-z0-9_-]+\.txt", name)]
    if "python/PYTHON.json" not in names or not selected or len(selected) > 100:
        raise BuildFailure("full archive lacks expected provenance and licenses")
    metadata_bytes = run(["/usr/bin/tar", "-xOf", str(archive), "python/PYTHON.json"], limit=2 * 1024 * 1024)
    metadata = json.loads(metadata_bytes)
    if metadata.get("python_version") != lock["python_version"] or metadata.get("target_triple") != lock["architectures"][architecture]["target_triple"]:
        raise BuildFailure("upstream metadata does not match runtime lock")
    deployment = metadata.get("apple_sdk_deployment_target", "")
    if not re.fullmatch(r"\d+\.\d+", deployment) or tuple(map(int, deployment.split("."))) > (14, 0):
        raise BuildFailure("upstream runtime requires macOS newer than 14.0")
    destination.mkdir(parents=True, mode=0o755)
    (destination / "PYTHON.json").write_bytes(metadata_bytes)
    (destination / "licenses").mkdir(mode=0o755)
    for name in sorted(selected):
        content = run(["/usr/bin/tar", "-xOf", str(archive), name], limit=1024 * 1024)
        (destination / "licenses" / PurePosixPath(name).name).write_bytes(content)


def app_inventory(app: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    total = 0
    for path in sorted(app.rglob("*")):
        relative = path.relative_to(app).as_posix()
        if path.is_symlink():
            raise BuildFailure("app inventory contains a symlink")
        if path.is_dir():
            path.chmod(0o755)
            directories.append({"path": relative, "mode": 0o755})
        elif path.is_file() and stat.S_ISREG(path.stat().st_mode):
            with path.open("rb") as handle:
                macho = handle.read(4) in MACHO_MAGICS
            mode = 0o755 if macho or path.stat().st_mode & 0o111 else 0o644
            path.chmod(mode)
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
                raise BuildFailure("app exceeds size bounds")
            files.append({"path": relative, "sha256": sha256(path), "bytes": size, "mode": mode, "mach_o": macho})
        else:
            raise BuildFailure("app inventory contains a special file")
        if len(files) + len(directories) > MAX_FILES:
            raise BuildFailure("app exceeds file count bound")
    return files, directories


def write_archive(app: Path, output: Path, files: list[dict[str, Any]], directories: list[dict[str, Any]]) -> None:
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        entries = [{"path": "", "mode": 0o755, "directory": True}]
        entries += [dict(item, directory=True) for item in directories]
        entries += [dict(item, directory=False) for item in files]
        for entry in sorted(entries, key=lambda item: item["path"]):
            name = APP_NAME + ("/" + entry["path"] if entry["path"] else "")
            info = zipfile.ZipInfo(name + ("/" if entry["directory"] else ""), ZIP_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((stat.S_IFDIR if entry["directory"] else stat.S_IFREG) | entry["mode"]) << 16
            archive.writestr(info, b"" if entry["directory"] else (app / entry["path"]).read_bytes(), compresslevel=9)


def build(args: argparse.Namespace) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise BuildFailure("Python runtime assembly requires a macOS build host")
    lock = load_lock()
    architecture = args.architecture
    app = safe_path(args.output_app)
    manifest = safe_path(args.write_manifest)
    archive = safe_path(args.write_archive)
    if app.name != APP_NAME:
        raise BuildFailure(f"app must be named {APP_NAME}")
    for output in (app, manifest, archive):
        if output.exists():
            raise BuildFailure(f"refusing to overwrite existing output: {output}")
    outputs = (app, manifest, archive)
    if len(set(outputs)) != 3 or any(left in right.parents for left in outputs for right in outputs if left != right):
        raise BuildFailure("manifest and archive must be distinct outputs outside the app")
    for commit in (args.source_commit, args.workflow_commit):
        if commit is not None and not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise BuildFailure("commit identities must be lowercase 40-character hashes")
    pins = lock["architectures"][architecture]
    install = verified_archive(pins["install_only_stripped"], args.install_archive, args.cache_directory)
    full = verified_archive(pins["full"], args.full_archive, args.cache_directory)
    for output in (app, manifest, archive):
        output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".python-runtime-", dir=app.parent))
    try:
        staged_app = staging / APP_NAME
        contents = staged_app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        resources = contents / "Resources"
        normalization = extract_install(install, resources / "python")
        if normalization["omitted_symlinks"] != list(OMITTED_SYMLINKS):
            raise BuildFailure("upstream runtime is missing expected interpreter aliases")
        extract_provenance(full, resources / "upstream", lock, architecture)
        template = safe_path(REPO_ROOT / "scripts/python_runtime_info.plist", regular=True)
        info = plistlib.loads(template.read_bytes())
        expected = {"CFBundleIdentifier": BUNDLE_IDENTIFIER, "CFBundleExecutable": "apple-reminders-python", "CFBundleShortVersionString": lock["python_version"], "CFBundleVersion": lock["release"], "LSMinimumSystemVersion": MINIMUM_MACOS, "CFBundlePackageType": "APPL"}
        if any(info.get(key) != value for key, value in expected.items()):
            raise BuildFailure("runtime Info.plist identity does not match lock")
        (contents / "Info.plist").write_bytes(plistlib.dumps(info, sort_keys=True))
        source = safe_path(REPO_ROOT / "scripts/python_runtime_launcher.c", regular=True)
        run(["/usr/bin/xcrun", "clang", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-arch", architecture, "-mmacosx-version-min=14.0", str(source), "-o", str(staged_app / EXECUTABLE_RELATIVE_PATH)])
        staged_app.chmod(0o755)
        files, directories = app_inventory(staged_app)
        source_paths = list(SOURCE_PATHS)
        if (REPO_ROOT / WORKFLOW_PATH).exists():
            source_paths.append(WORKFLOW_PATH)
        staged_archive = staging / "unsigned.zip"
        write_archive(staged_app, staged_archive, files, directories)
        receipt = {"schema_version": 1, "architecture": architecture, "target_triple": pins["target_triple"], "runtime_version": lock["python_version"], "runtime_build": lock["release"], "app_name": APP_NAME, "bundle_identifier": BUNDLE_IDENTIFIER, "executable_relative_path": EXECUTABLE_RELATIVE_PATH, "source_commit": args.source_commit, "workflow_commit": args.workflow_commit, "source_input_sha256": {name: sha256(safe_path(REPO_ROOT / name, regular=True)) for name in source_paths}, "upstream": {kind: pins[kind] for kind in ("install_only_stripped", "full")}, "normalization": normalization, "files": files, "directories": directories, "macho_paths": [entry["path"] for entry in files if entry["mach_o"]], "unsigned_archive_sha256": sha256(staged_archive), "unsigned_archive_bytes": staged_archive.stat().st_size}
        staged_manifest = staging / "runtime-build.json"
        staged_manifest.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for output in (app, manifest, archive):
            if safe_path(output).exists():
                raise BuildFailure("output appeared while runtime was building")
        os.replace(staged_app, app)
        shutil.move(str(staged_archive), str(archive))
        shutil.move(str(staged_manifest), str(manifest))
        return {"app": str(app), "manifest": str(manifest), "archive": str(archive), "architecture": architecture, "sha256": receipt["unsigned_archive_sha256"], "bytes": receipt["unsigned_archive_bytes"]}
    finally:
        shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--output-app", type=Path, required=True)
    parser.add_argument("--write-manifest", type=Path, required=True)
    parser.add_argument("--write-archive", type=Path, required=True)
    parser.add_argument("--cache-directory", type=Path, default=Path.home() / "Library/Caches/apple-reminders-build/python")
    parser.add_argument("--install-archive", type=Path)
    parser.add_argument("--full-archive", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--workflow-commit")
    args = parser.parse_args(argv)
    try:
        result = build(args)
    except (BuildFailure, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Python runtime build failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
