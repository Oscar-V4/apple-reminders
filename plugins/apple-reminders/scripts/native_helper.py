"""Fail-closed, compiler-free resolution of the signed Native helper release.

Shares the bounded file reader and pure Mach-O parser with EventKit. Verification
never launches a Native helper or accesses a Reminders store.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import stat
import sys

import eventkit_bridge as trust

NativeHelperUnavailable = trust.BundledHelperUnavailable
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "AppleRemindersNativeHelper.app"
BUNDLE_ID = "io.github.oscar-v4.apple-reminders.native-helper"
TEAM_ID = "V8347N9346"
EXECUTABLES = {kind: f"apple-reminders-{name}-helper" for kind, name in (
    ("image", "image"), ("sections", "sections"), ("recovery", "recovery"))}
SOURCES = ("scripts/remkit_attach_image.m", "scripts/remkit_sections.m", "scripts/remkit_recover.m")
BUILD_INPUTS = tuple("scripts/" + name for name in (
    "build_native_helper_app.py", "verify_native_helper.py",
    "prepare_signed_native_helper.sh", "native_helper_app_info.plist"))
SOURCE_BUILD_ENV = "APPLE_REMINDERS_NATIVE_ALLOW_SOURCE_BUILD"


def source_build_enabled() -> bool:
    return os.environ.get(SOURCE_BUILD_ENV) == "1"


def _directory(path: Path) -> None:
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o755:
        raise NativeHelperUnavailable("Native helper directory is unsafe")


def _inventory(app: Path) -> dict[str, str]:
    expected = {"Contents/Info.plist": 0o644, "Contents/_CodeSignature/CodeResources": 0o644}
    expected.update({f"Contents/MacOS/{name}": 0o755 for name in EXECUTABLES.values()})
    dirs = {"Contents", "Contents/MacOS", "Contents/_CodeSignature"}
    found, found_dirs = {}, set()
    _directory(app.parent)
    _directory(app)
    for root, directories, files in os.walk(app, followlinks=False):
        for name in directories:
            path = Path(root) / name
            relative = path.relative_to(app).as_posix()
            if relative not in dirs:
                raise NativeHelperUnavailable("Native helper directory inventory is invalid")
            _directory(path)
            found_dirs.add(relative)
        for name in files:
            path = Path(root) / name
            relative = path.relative_to(app).as_posix()
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != expected.get(relative):
                raise NativeHelperUnavailable("Native helper file inventory or mode is invalid")
            found[f"{APP_NAME}/{relative}"] = trust._sha256_regular_file(path, "Native helper member")
    if found_dirs != dirs or set(found) != {f"{APP_NAME}/{p}" for p in expected}:
        raise NativeHelperUnavailable("Native helper bundle inventory is invalid")
    return found


def _signature(app: Path) -> None:
    requirement = (f'anchor apple generic and identifier "{BUNDLE_ID}"'
        ' and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
        f' and certificate leaf[subject.OU] = "{TEAM_ID}"'
        ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists')
    trust._run_bundled_helper_check(["/usr/bin/codesign", "--verify", "--deep", "--strict",
        "--test-requirement", "=" + requirement, str(app)])
    # Inspect every nested executable as well as the enclosing application.
    for path in [app, *(app / "Contents/MacOS" / n for n in EXECUTABLES.values())]:
        out, err = trust._run_bundled_helper_check(["/usr/bin/codesign", "-dvvv", str(path)])
        details = out + "\n" + err
        if trust._codesign_value(details, "TeamIdentifier") != TEAM_ID:
            raise NativeHelperUnavailable("Native helper signing team is invalid")
        if trust._codesign_value(details, "Identifier") != BUNDLE_ID:
            raise NativeHelperUnavailable("Native helper signing identity is invalid")
        if not any(line.startswith("CodeDirectory ") and "runtime" in line for line in details.splitlines()):
            raise NativeHelperUnavailable("Native helper lacks Hardened Runtime")
        if trust._codesign_value(details, "Timestamp") is None:
            raise NativeHelperUnavailable("Native helper lacks a secure timestamp")


def resolve_helper(kind: str = "image") -> Path:
    """Verify all members on each resolution; never silently compile a fallback."""
    if kind not in EXECUTABLES:
        raise ValueError("Unknown Native helper kind")
    if sys.platform != "darwin" or platform.machine() not in {"arm64", "x86_64"}:
        raise NativeHelperUnavailable("Native helper requires a supported macOS architecture")
    app = PLUGIN_ROOT / "native" / APP_NAME
    try:
        inventory = _inventory(app)
        manifest = json.loads(trust._read_regular_file(app.parent / "native-helper-build.json", "Native provenance"))
        version = json.loads(trust._read_regular_file(PLUGIN_ROOT / ".codex-plugin/plugin.json", "plugin manifest"))["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise NativeHelperUnavailable("Native plugin version is invalid")
        metadata = {
            "schema_version": 1, "app_name": APP_NAME, "architectures": ["arm64", "x86_64"],
            "bundle_identifier": BUNDLE_ID, "team_id": TEAM_ID, "signature": "developer-id",
            "executable": EXECUTABLES["image"], "executables": EXECUTABLES,
            "minimum_macos": "14.0", "minimum_macos_by_architecture": {
                kind: {"arm64": "14.0", "x86_64": "14.0"} for kind in EXECUTABLES},
            "plugin_version": version, "notarized": True, "notarization_checked": True,
            "app_files": inventory,
            "source_files": {p: trust._sha256_regular_file(PLUGIN_ROOT / p, "Native source") for p in SOURCES},
            "binary_sha256": {k: inventory[f"{APP_NAME}/Contents/MacOS/{n}"] for k,n in EXECUTABLES.items()},
        }
        if not isinstance(manifest, dict) or set(manifest) != set(metadata) | {"source_commit", "workflow_commit", "build_inputs", "build_environment"}:
            raise NativeHelperUnavailable("Native provenance key inventory is invalid")
        for key, expected in metadata.items():
            if manifest.get(key) != expected or type(manifest.get(key)) is not type(expected):
                raise NativeHelperUnavailable(f"Native provenance {key} is invalid")
        for key in ("source_commit", "workflow_commit"):
            if not isinstance(manifest[key], str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", manifest[key]):
                raise NativeHelperUnavailable("Native commit provenance is invalid")
        inputs = manifest["build_inputs"]
        if not isinstance(inputs, dict) or set(inputs) != set(BUILD_INPUTS) or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in inputs.values()):
            raise NativeHelperUnavailable("Native build input provenance is invalid")
        environment = manifest["build_environment"]
        if not isinstance(environment, dict) or set(environment) != {"clang", "linker", "macos_sdk", "xcode_path", "macos_sdk_path"} or any(not isinstance(v, str) or not v.strip() for v in environment.values()):
            raise NativeHelperUnavailable("Native build environment is invalid")
        info = plistlib.loads(trust._read_regular_file(app / "Contents/Info.plist", "Native Info.plist"))
        for key, value in {"CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": EXECUTABLES["image"], "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version, "CFBundleVersion": version, "LSMinimumSystemVersion": "14.0"}.items():
            if info.get(key) != value:
                raise NativeHelperUnavailable("Native bundle metadata is invalid")
        for name in EXECUTABLES.values():
            data = trust._read_regular_file(app / "Contents/MacOS" / name, "Native executable")
            if hashlib.sha256(data).hexdigest() != inventory[f"{APP_NAME}/Contents/MacOS/{name}"]:
                raise NativeHelperUnavailable("Native executable changed during verification")
            trust._verify_bundled_helper_architectures(data)
        _signature(app)
        if _inventory(app) != inventory:
            raise NativeHelperUnavailable("Native bundle changed during verification")
        return app / "Contents/MacOS" / EXECUTABLES[kind]
    except NativeHelperUnavailable:
        raise
    except Exception as exc:
        raise NativeHelperUnavailable("Native helper could not be verified") from exc
