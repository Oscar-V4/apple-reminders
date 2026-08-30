#!/usr/bin/env python3
"""Verify a built, signed, and optionally notarized EventKit helper app.

The default verification path is structural and never launches the helper.
Use ``--run-protocol-probes`` only in a credentials-free job after all signing
and notarization credentials have been removed from the environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path
from typing import Any


from build_eventkit_helper_app import (
    APP_NAME,
    ARCHITECTURES,
    BUILD_INPUT_RELATIVE_PATHS,
    BUNDLE_IDENTIFIER,
    CODESIGN,
    DEFAULT_PLUGIN_ROOT,
    EXECUTABLE_NAME,
    MANIFEST_RELATIVE_PATH,
    MINIMUM_MACOS,
    REPO_ROOT,
    SCHEMA_RELATIVE_PATH,
    SOURCE_RELATIVE_PATH,
    XCRUN,
    BuildFailure,
    plugin_version,
    run,
    versioned_info_plist,
)


MANIFEST_SCHEMA_VERSION = 1
STAPLER = "/usr/bin/xcrun"
SPCTL = "/usr/sbin/spctl"


def validated_source_commit(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value):
        raise BuildFailure("source commit must be a full lowercase Git object ID")
    return value


def current_build_environment() -> dict[str, str]:
    commands = {
        "clang": [XCRUN, "clang", "--version"],
        "linker": [XCRUN, "ld", "-v"],
        "macos_sdk": [XCRUN, "--sdk", "macosx", "--show-sdk-version"],
        "xcode_path": ["/usr/bin/xcode-select", "-p"],
    }
    environment: dict[str, str] = {}
    for key, argv in commands.items():
        completed = run(argv)
        value = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        if not value:
            raise BuildFailure(f"could not capture build environment field: {key}")
        environment[key] = value
    return environment


def sha256(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise BuildFailure(f"provenance input is missing or unsafe: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _app_inventory(app: Path) -> tuple[dict[str, str], set[str]]:
    files: dict[str, str] = {}
    directories: set[str] = set()
    for path in sorted(app.rglob("*")):
        relative = path.relative_to(app.parent).as_posix()
        if path.is_symlink():
            raise BuildFailure(f"helper app contains a symlink: {path.relative_to(app)}")
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_dir():
            if mode != 0o755:
                raise BuildFailure(f"helper directory mode drift: {relative} is {mode:04o}")
            directories.add(relative)
        elif path.is_file():
            files[relative] = sha256(path)
        else:
            raise BuildFailure(f"helper app contains a special file: {relative}")
    return files, directories


def _protocol_probe(binary: Path, operation: str) -> None:
    completed = run(
        [str(binary)],
        input_text=json.dumps(
            {"schema_version": 1, "operation": operation},
            separators=(",", ":"),
        ),
        timeout=20,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BuildFailure(f"{operation} probe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BuildFailure(f"{operation} probe returned a non-object receipt")
    required_data_fields = {
        "schema": {"operations", "request_schema_version", "statuses"},
        "capabilities": {"backend", "fields", "reads", "safety", "writes"},
    }
    data = payload.get("data")
    if (
        payload.get("schema_version") != 1
        or payload.get("operation") != operation
        or payload.get("status") != "verified"
        or payload.get("ok") is not True
        or not isinstance(data, dict)
        or not required_data_fields[operation].issubset(data)
    ):
        raise BuildFailure(f"{operation} probe returned an unexpected receipt: {payload}")


def _minimum_version(binary: Path, architecture: str) -> str:
    completed = run(
        [XCRUN, "vtool", "-show-build", "-arch", architecture, str(binary)]
    )
    match = re.search(r"^\s*minos\s+([^\s]+)\s*$", completed.stdout, re.MULTILINE)
    if not match:
        raise BuildFailure(f"could not read {architecture} minimum macOS version")
    return match.group(1)


def _codesign_details(app: Path) -> str:
    completed = subprocess.run(
        [CODESIGN, "-dvvv", str(app)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise BuildFailure((completed.stderr or completed.stdout).strip())
    return completed.stderr


def _value_from_codesign(details: str, key: str) -> str | None:
    prefix = f"{key}="
    value = next(
        (
            line.partition("=")[2].strip()
            for line in details.splitlines()
            if line.startswith(prefix)
        ),
        None,
    )
    return None if value in {None, "", "not set"} else value


def _verify_no_entitlements(app: Path) -> None:
    completed = subprocess.run(
        [CODESIGN, "-d", "--entitlements", ":-", str(app)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    combined = completed.stdout + "\n" + completed.stderr
    entitlement_payload = "\n".join(
        line
        for line in combined.splitlines()
        if line
        and not line.startswith("Executable=")
        and "warning: Specifying ':' in the path is deprecated" not in line
    ).strip()
    if entitlement_payload:
        raise BuildFailure(f"helper has unreviewed entitlements: {entitlement_payload}")


def verify_app(
    plugin_root: Path,
    app: Path,
    *,
    expected_team_id: str | None = None,
    require_developer_id: bool = False,
    require_notarized: bool = False,
    run_protocol_probes: bool = False,
) -> dict[str, Any]:
    """Verify one app, opting into native execution only when explicitly asked."""

    plugin_root = plugin_root.expanduser().resolve()
    app = Path(os.path.abspath(os.fspath(app.expanduser())))
    if app.name != APP_NAME or not app.is_dir() or app.is_symlink():
        raise BuildFailure(f"expected a regular {APP_NAME} bundle: {app}")
    if require_notarized:
        require_developer_id = True
        if not expected_team_id:
            raise BuildFailure("notarized verification requires an expected Team ID")
    if expected_team_id and not re.fullmatch(r"[A-Z0-9]{10}", expected_team_id):
        raise BuildFailure(
            "expected Team ID must contain exactly 10 uppercase letters or digits"
        )

    info_path = app / "Contents" / "Info.plist"
    binary = app / "Contents" / "MacOS" / EXECUTABLE_NAME
    if not info_path.is_file() or info_path.is_symlink():
        raise BuildFailure("helper app has no regular Contents/Info.plist")
    if not binary.is_file() or binary.is_symlink():
        raise BuildFailure("helper app has no regular executable")

    actual_info_bytes = info_path.read_bytes()
    expected_info_bytes = versioned_info_plist(plugin_root)
    if actual_info_bytes != expected_info_bytes:
        raise BuildFailure("helper Info.plist does not exactly match reviewed source")
    try:
        info = plistlib.loads(actual_info_bytes)
    except plistlib.InvalidFileException as exc:
        raise BuildFailure(f"helper Info.plist is invalid: {exc}") from exc
    current_plugin_version = plugin_version(plugin_root)
    expected_info = {
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": current_plugin_version,
        "CFBundleVersion": current_plugin_version,
        "LSMinimumSystemVersion": MINIMUM_MACOS,
    }
    drift = {
        key: {"expected": value, "actual": info.get(key)}
        for key, value in expected_info.items()
        if info.get(key) != value
    }
    if drift:
        raise BuildFailure(f"helper Info.plist drift: {drift}")
    if not info.get("NSRemindersFullAccessUsageDescription"):
        raise BuildFailure("helper Info.plist lacks the Reminders purpose string")

    lipo = run([XCRUN, "lipo", "-archs", str(binary)]).stdout.split()
    if set(lipo) != set(ARCHITECTURES) or len(lipo) != len(ARCHITECTURES):
        raise BuildFailure(f"helper architectures are {lipo}, expected {ARCHITECTURES}")
    minimum_versions = {
        architecture: _minimum_version(binary, architecture)
        for architecture in ARCHITECTURES
    }
    if set(minimum_versions.values()) != {MINIMUM_MACOS}:
        raise BuildFailure(f"helper minimum macOS drift: {minimum_versions}")

    app_files, app_directories = _app_inventory(app)
    app_mode = stat.S_IMODE(app.lstat().st_mode)
    if app_mode != 0o755:
        raise BuildFailure(f"helper app directory mode drift: {APP_NAME} is {app_mode:04o}")
    expected_inventory = {
        f"{APP_NAME}/Contents/Info.plist",
        f"{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}",
        f"{APP_NAME}/Contents/_CodeSignature/CodeResources",
    }
    if require_notarized:
        expected_inventory.add(f"{APP_NAME}/Contents/CodeResources")
    if set(app_files) != expected_inventory:
        raise BuildFailure(
            "helper app file inventory drift: "
            f"expected {sorted(expected_inventory)}, got {sorted(app_files)}"
        )
    expected_directories = {
        f"{APP_NAME}/Contents",
        f"{APP_NAME}/Contents/MacOS",
        f"{APP_NAME}/Contents/_CodeSignature",
    }
    if app_directories != expected_directories:
        raise BuildFailure(
            "helper app directory inventory drift: "
            f"expected {sorted(expected_directories)}, got {sorted(app_directories)}"
        )
    expected_modes = {
        f"{APP_NAME}/Contents/Info.plist": 0o644,
        f"{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}": 0o755,
        f"{APP_NAME}/Contents/_CodeSignature/CodeResources": 0o644,
    }
    if require_notarized:
        expected_modes[f"{APP_NAME}/Contents/CodeResources"] = 0o644
    for relative, expected_mode in expected_modes.items():
        actual_mode = stat.S_IMODE((app.parent / relative).stat().st_mode)
        if actual_mode != expected_mode:
            raise BuildFailure(
                f"helper file mode drift: {relative} is {actual_mode:04o}, "
                f"expected {expected_mode:04o}"
            )

    run(
        [
            CODESIGN,
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(app),
        ]
    )
    signing = _codesign_details(app)
    identifier = _value_from_codesign(signing, "Identifier")
    team_id = _value_from_codesign(signing, "TeamIdentifier")
    if identifier != BUNDLE_IDENTIFIER:
        raise BuildFailure(f"codesign identifier drift: {identifier}")
    if expected_team_id and team_id != expected_team_id:
        raise BuildFailure(
            f"codesign Team ID mismatch: expected {expected_team_id}, got {team_id}"
        )
    if require_developer_id:
        if not expected_team_id:
            raise BuildFailure("Developer ID verification requires an expected Team ID")
        requirement = (
            f'anchor apple generic and identifier "{BUNDLE_IDENTIFIER}" '
            "and certificate 1[field.1.2.840.113635.100.6.2.6] exists "
            f'and certificate leaf[subject.OU] = "{expected_team_id}" '
            "and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
        )
        run(
            [
                CODESIGN,
                "--verify",
                "--deep",
                "--strict",
                "--test-requirement",
                f"={requirement}",
                str(app),
            ]
        )
        if "flags=0x10000(runtime)" not in signing:
            raise BuildFailure("helper signature lacks Hardened Runtime")
        if "Timestamp=" not in signing:
            raise BuildFailure("helper signature lacks a secure timestamp")
        if not team_id:
            raise BuildFailure("Developer ID helper has no TeamIdentifier")

    _verify_no_entitlements(app)
    if require_notarized:
        run([STAPLER, "stapler", "validate", str(app)], timeout=120)
        run([SPCTL, "--assess", "--type", "execute", "-vv", str(app)], timeout=120)
    if run_protocol_probes:
        for operation in ("schema", "capabilities"):
            _protocol_probe(binary, operation)

    return {
        "app_name": APP_NAME,
        "app_files": app_files,
        "architectures": sorted(ARCHITECTURES),
        "binary_sha256": sha256(binary),
        "bundle_identifier": BUNDLE_IDENTIFIER,
        "executable": EXECUTABLE_NAME,
        "minimum_macos": MINIMUM_MACOS,
        "minimum_macos_by_architecture": minimum_versions,
        "notarization_checked": require_notarized,
        "notarized": True if require_notarized else None,
        "plugin_version": current_plugin_version,
        "signature": "developer-id" if require_developer_id else "unchecked",
        "team_id": team_id,
    }


def build_manifest(
    plugin_root: Path,
    verification: dict[str, Any],
    *,
    source_commit: str,
    build_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    source_commit = validated_source_commit(source_commit)
    source_files = {
        relative.as_posix(): sha256(plugin_root / relative)
        for relative in (
            MANIFEST_RELATIVE_PATH,
            SOURCE_RELATIVE_PATH,
            SCHEMA_RELATIVE_PATH,
        )
    }
    build_inputs = {
        relative.as_posix(): sha256(REPO_ROOT / relative)
        for relative in BUILD_INPUT_RELATIVE_PATHS
    }
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_commit": source_commit,
        "source_files": source_files,
        "build_inputs": build_inputs,
        "build_environment": build_environment or current_build_environment(),
        **verification,
    }


def verify_manifest(
    plugin_root: Path,
    manifest_path: Path,
    actual: dict[str, Any],
    *,
    expected_source_commit: str,
) -> dict[str, Any]:
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BuildFailure("native helper manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildFailure(f"could not read native helper manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BuildFailure("native helper manifest root must be an object")
    manifest_build_environment = manifest.get("build_environment")
    expected_environment_keys = {"clang", "linker", "macos_sdk", "xcode_path"}
    if (
        not isinstance(manifest_build_environment, dict)
        or set(manifest_build_environment) != expected_environment_keys
        or any(
            not isinstance(value, str) or not value.strip()
            for value in manifest_build_environment.values()
        )
    ):
        raise BuildFailure("native helper manifest has invalid build environment provenance")
    expected = build_manifest(
        plugin_root,
        actual,
        source_commit=validated_source_commit(expected_source_commit),
        build_environment=manifest_build_environment,
    )
    if manifest != expected:
        raise BuildFailure("native helper manifest does not match source and app bytes")
    return manifest


def _write_new_manifest(path: Path, payload: dict[str, Any]) -> Path:
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    if path.exists() or path.is_symlink():
        raise BuildFailure(f"refusing to overwrite existing manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise BuildFailure(f"temporary manifest path already exists: {temporary}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, default=DEFAULT_PLUGIN_ROOT)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--expected-team-id")
    parser.add_argument("--require-developer-id", action="store_true")
    parser.add_argument("--require-notarized", action="store_true")
    parser.add_argument(
        "--run-protocol-probes",
        action="store_true",
        help="launch the helper for schema/capability probes (credentials-free jobs only)",
    )
    parser.add_argument("--write-manifest", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source-commit", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args(argv)
    try:
        verification = verify_app(
            args.plugin_root,
            args.app,
            expected_team_id=args.expected_team_id,
            require_developer_id=args.require_developer_id,
            require_notarized=args.require_notarized,
            run_protocol_probes=args.run_protocol_probes,
        )
        result: dict[str, Any] = {
            "protocol_probes_run": args.run_protocol_probes,
            "verification": verification,
        }
        if args.write_manifest:
            if not args.source_commit:
                raise BuildFailure("--source-commit is required when writing a manifest")
            manifest = build_manifest(
                args.plugin_root.resolve(),
                verification,
                source_commit=args.source_commit,
            )
            result["manifest"] = str(
                _write_new_manifest(args.write_manifest, manifest)
            )
        if args.manifest:
            if not args.source_commit:
                raise BuildFailure(
                    "--source-commit or GITHUB_SHA is required when verifying a manifest"
                )
            result["validated_manifest"] = verify_manifest(
                args.plugin_root.resolve(),
                Path(os.path.abspath(os.fspath(args.manifest.expanduser()))),
                verification,
                expected_source_commit=args.source_commit,
            )["source_commit"]
    except (BuildFailure, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"EventKit helper verification failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
