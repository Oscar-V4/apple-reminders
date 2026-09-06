#!/usr/bin/env python3
"""Main-owned, non-executing Native signing artifact operations."""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import zipfile
from pathlib import Path

from build_native_helper_app import APP_NAME, EXECUTABLES, DEFAULT_PLUGIN_ROOT, BuildFailure
from verify_native_helper import (
    build_manifest, sha256, verify_app, verify_manifest, _write_new_manifest,
)

ARCHIVE_NAME = "AppleRemindersNativeHelper-notarized.zip"
MANIFEST_NAME = "native-helper-build.json"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
BASE_MODES = {
    f"{APP_NAME}/Contents/Info.plist": 0o644,
    f"{APP_NAME}/Contents/_CodeSignature/CodeResources": 0o644,
    **{f"{APP_NAME}/Contents/MacOS/{name}": 0o755 for name in EXECUTABLES.values()},
}
TICKET_NAME = f"{APP_NAME}/Contents/CodeResources"


def read_context(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise BuildFailure("unsigned context is missing, unsafe, or too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BuildFailure("unsigned context must be an object")
    return value


def archive_app(app: Path, target: Path, *, notarized: bool) -> None:
    expected = dict(BASE_MODES)
    if notarized:
        expected[TICKET_NAME] = 0o644
    if target.exists() or target.is_symlink():
        raise BuildFailure("refusing to overwrite artifact")
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_STORED) as handle:
        for relative, mode in sorted(expected.items()):
            path = app.parent / relative
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MEMBER_BYTES:
                raise BuildFailure("invalid Native archive member")
            info = zipfile.ZipInfo(relative, date_time=(2024, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            handle.writestr(info, path.read_bytes())
    if target.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BuildFailure("Native archive exceeds size limit")


def extract_app(archive: Path, destination: Path, expected_sha256: str, *, notarized: bool) -> Path:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES or sha256(archive) != expected_sha256:
        raise BuildFailure("Native archive size or digest mismatch")
    expected = dict(BASE_MODES)
    if notarized:
        expected[TICKET_NAME] = 0o644
    # Inspect the complete directory and modes before writing any archive data.
    with zipfile.ZipFile(archive) as handle:
        infos = handle.infolist()
        if len(infos) != len(expected) or {item.filename for item in infos} != set(expected):
            raise BuildFailure("Native archive member inventory mismatch")
        for info in infos:
            if (info.create_system != 3
                    or info.external_attr >> 16 != stat.S_IFREG | expected[info.filename]
                    or not 0 < info.file_size <= MAX_MEMBER_BYTES
                    or info.flag_bits & 1 or info.compress_type != zipfile.ZIP_STORED):
                raise BuildFailure("Native archive member mode or format mismatch")
        destination.mkdir(mode=0o700)
        for info in infos:
            target = destination / info.filename
            for directory in reversed(target.parents):
                if destination in directory.parents:
                    directory.mkdir(mode=0o755, exist_ok=True)
                    directory.chmod(0o755)
            with handle.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(expected[info.filename])
    return destination / APP_NAME


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("unsigned", "extract", "verify-unsigned", "finalize"))
    parser.add_argument("--plugin-root", type=Path, default=DEFAULT_PLUGIN_ROOT)
    parser.add_argument("--app", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--notarized", action="store_true")
    parser.add_argument("--context", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--workflow-commit")
    args = parser.parse_args()
    try:
        if args.operation == "extract":
            if not all((args.archive, args.output, args.sha256)):
                raise BuildFailure("extract requires archive, output, and sha256")
            extract_app(args.archive, args.output, args.sha256, notarized=args.notarized)
            return
        if not all((args.app, args.source_commit, args.workflow_commit)):
            raise BuildFailure("app and exact source/workflow commits are required")
        actual = verify_app(args.plugin_root, args.app,
            expected_team_id="V8347N9346" if args.operation == "finalize" else None,
            require_developer_id=args.operation == "finalize",
            require_notarized=args.operation == "finalize")
        if args.operation == "verify-unsigned":
            if not args.context:
                raise BuildFailure("unsigned verification requires context")
            read_context(args.context)
            verify_manifest(args.plugin_root, args.context, actual,
                expected_source_commit=args.source_commit,
                expected_workflow_commit=args.workflow_commit)
            return
        if not args.output:
            raise BuildFailure("artifact output directory is required")
        args.output.mkdir(mode=0o755)
        environment = None
        if args.operation == "finalize":
            if not args.context:
                raise BuildFailure("finalization requires original unsigned context")
            context = read_context(args.context)
            # This context was verified against source and the unsigned app
            # before signing; retain its build machine/SDK provenance.
            if (context.get("source_commit") != args.source_commit
                    or context.get("workflow_commit") != args.workflow_commit):
                raise BuildFailure("unsigned context identity mismatch")
            environment = context["build_environment"]
        manifest = build_manifest(args.plugin_root, actual,
            source_commit=args.source_commit, workflow_commit=args.workflow_commit,
            build_environment=environment)
        name = MANIFEST_NAME if args.operation == "finalize" else "unsigned-build-context.json"
        _write_new_manifest(args.output / name, manifest)
        archive_name = ARCHIVE_NAME if args.operation == "finalize" else "unsigned.zip"
        archive_app(args.app, args.output / archive_name, notarized=args.operation == "finalize")
        (args.output / "SHA256SUMS").write_text(
            "".join(f"{sha256(args.output / name)}  {name}\n" for name in (archive_name, name)),
            encoding="utf-8")
    except (BuildFailure, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Native artifact preparation failed: {exc}\n")


if __name__ == "__main__":
    main()
