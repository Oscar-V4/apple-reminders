#!/usr/bin/env python3
"""Build and sign the universal macOS EventKit helper app.

The build is deliberately non-executing: it compiles, assembles, signs, and
inspects the helper, but it never launches repository-controlled native code.
Protocol probes belong in a separate, credentials-free verification job.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
APP_NAME = "AppleRemindersEventKitHelper.app"
EXECUTABLE_NAME = "apple-reminders-eventkit-helper"
BUNDLE_IDENTIFIER = "io.github.oscar-v4.apple-reminders.eventkit-bridge"
MINIMUM_MACOS = "14.0"
ARCHITECTURES = ("arm64", "x86_64")

# Plugin-relative inputs define the native helper's behavior.
SOURCE_RELATIVE_PATH = Path("scripts/reminders_eventkit.m")
SCHEMA_RELATIVE_PATH = Path("scripts/eventkit_bridge_schema.json")
MANIFEST_RELATIVE_PATH = Path(".codex-plugin/plugin.json")

# Repository-relative packaging inputs are intentionally kept out of the
# shipped plugin until the signed helper is ready to land atomically.
INFO_TEMPLATE_RELATIVE_PATH = Path("scripts/eventkit_helper_app_info.plist")
BUILD_INPUT_RELATIVE_PATHS = (
    INFO_TEMPLATE_RELATIVE_PATH,
    Path("scripts/build_eventkit_helper_app.py"),
    Path("scripts/verify_eventkit_helper.py"),
    Path("scripts/prepare_signed_eventkit_helper.sh"),
    Path(".github/workflows/prepare-signed-helper-source.yml"),
)

XCRUN = "/usr/bin/xcrun"
CODESIGN = "/usr/bin/codesign"


class BuildFailure(RuntimeError):
    """A deterministic helper build or validation step failed."""


def run(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded build command and retain useful failure diagnostics."""

    completed = subprocess.run(
        list(argv),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        diagnostics = (completed.stderr or completed.stdout).strip()
        rendered = " ".join(str(item) for item in argv)
        raise BuildFailure(
            f"command failed with exit code {completed.returncode}: {rendered}\n"
            f"{diagnostics[-8000:]}"
        )
    return completed


def _regular_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise BuildFailure(f"{label} is missing or unsafe: {path}")
    return path


def plugin_version(plugin_root: Path) -> str:
    manifest_path = _regular_file(
        plugin_root / MANIFEST_RELATIVE_PATH,
        "plugin manifest",
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildFailure(f"could not read plugin manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise BuildFailure("plugin manifest root must be an object")
    version = payload.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise BuildFailure(
            "plugin version must use exactly three numeric components"
        )
    return version


def versioned_info_plist(plugin_root: Path) -> bytes:
    """Render the reviewed app plist with the current plugin version."""

    info_path = _regular_file(
        REPO_ROOT / INFO_TEMPLATE_RELATIVE_PATH,
        "helper app Info.plist template",
    )
    try:
        payload: dict[str, Any] = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise BuildFailure(f"could not read helper app Info.plist template: {exc}") from exc

    if not isinstance(payload, dict):
        raise BuildFailure("helper app Info.plist root must be a dictionary")
    expected = {
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": MINIMUM_MACOS,
    }
    drift = {
        key: {"expected": value, "actual": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if drift:
        raise BuildFailure(f"reviewed helper app Info.plist drift: {drift}")
    if not payload.get("NSRemindersFullAccessUsageDescription"):
        raise BuildFailure(
            "helper app Info.plist lacks NSRemindersFullAccessUsageDescription"
        )

    version = plugin_version(plugin_root)
    payload["CFBundleShortVersionString"] = version
    payload["CFBundleVersion"] = version
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _team_identifier(signing_details: str) -> str | None:
    for line in signing_details.splitlines():
        if line.startswith("TeamIdentifier="):
            value = line.partition("=")[2].strip()
            return None if value in {"", "not set"} else value
    return None


def build_app(
    plugin_root: Path,
    output_app: Path,
    *,
    identity: str,
    expected_team_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build one app without ever launching its executable."""

    plugin_root = plugin_root.expanduser().resolve()
    output_app = Path(os.path.abspath(os.fspath(output_app.expanduser())))
    if output_app.name != APP_NAME:
        raise BuildFailure(f"output app must be named exactly {APP_NAME}")
    if output_app.is_symlink():
        raise BuildFailure("refusing to replace a symlinked output app")
    if expected_team_id and not re.fullmatch(r"[A-Z0-9]{10}", expected_team_id):
        raise BuildFailure(
            "expected Team ID must contain exactly 10 uppercase letters or digits"
        )
    if identity == "-" and expected_team_id:
        raise BuildFailure("an ad-hoc build cannot have an expected Team ID")

    output_existed = output_app.exists()
    original_output_identity: tuple[int, int] | None = None
    if output_existed:
        if not force:
            raise BuildFailure(f"refusing to overwrite existing app: {output_app}")
        if not output_app.is_dir():
            raise BuildFailure(f"existing output is not an app directory: {output_app}")
        original_stat = output_app.lstat()
        original_output_identity = (original_stat.st_dev, original_stat.st_ino)

    source = _regular_file(
        plugin_root / SOURCE_RELATIVE_PATH,
        "reviewed EventKit source",
    )
    info_bytes = versioned_info_plist(plugin_root)

    output_app.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".eventkit-helper-", dir=output_app.parent)
    )
    try:
        staged_app = staging_root / APP_NAME
        contents = staged_app / "Contents"
        macos = contents / "MacOS"
        slices = staging_root / "slices"
        macos.mkdir(parents=True)
        slices.mkdir()
        info_path = contents / "Info.plist"
        info_path.write_bytes(info_bytes)
        staged_app.chmod(0o755)
        contents.chmod(0o755)
        macos.chmod(0o755)
        info_path.chmod(0o644)

        slice_paths: list[Path] = []
        for architecture in ARCHITECTURES:
            slice_path = slices / architecture
            run(
                [
                    XCRUN,
                    "clang",
                    "-fobjc-arc",
                    "-fblocks",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-Wno-deprecated-declarations",
                    f"-mmacosx-version-min={MINIMUM_MACOS}",
                    "-arch",
                    architecture,
                    "-framework",
                    "Foundation",
                    "-framework",
                    "EventKit",
                    "-framework",
                    "CoreLocation",
                    str(source),
                    "-sectcreate",
                    "__TEXT",
                    "__info_plist",
                    str(info_path),
                    "-o",
                    str(slice_path),
                ]
            )
            slice_paths.append(slice_path)

        binary = macos / EXECUTABLE_NAME
        run(
            [
                XCRUN,
                "lipo",
                "-create",
                *(str(path) for path in slice_paths),
                "-output",
                str(binary),
            ]
        )
        binary.chmod(0o755)

        sign_command = [CODESIGN, "--force", "--sign", identity]
        if identity != "-":
            sign_command[1:1] = ["--options", "runtime", "--timestamp"]
        sign_command.append(str(staged_app))
        previous_umask = os.umask(0o022)
        try:
            run(sign_command, timeout=300)
        finally:
            os.umask(previous_umask)

        run(
            [
                CODESIGN,
                "--verify",
                "--deep",
                "--strict",
                "--verbose=2",
                str(staged_app),
            ]
        )
        run([XCRUN, "lipo", str(binary), "-verify_arch", *ARCHITECTURES])
        signing = run([CODESIGN, "-dvvv", str(staged_app)]).stderr
        actual_team_id = _team_identifier(signing)
        if expected_team_id and actual_team_id != expected_team_id:
            raise BuildFailure(
                "signed helper Team ID mismatch: "
                f"expected {expected_team_id}, got {actual_team_id or 'none'}"
            )

        previous_app: Path | None = None
        if output_existed:
            if not output_app.is_dir() or output_app.is_symlink():
                raise BuildFailure("existing output changed during the build")
            current_stat = output_app.lstat()
            if (current_stat.st_dev, current_stat.st_ino) != original_output_identity:
                raise BuildFailure("existing output was replaced during the build")
            previous_app = staging_root / ".previous-helper.app"
            os.replace(output_app, previous_app)
        elif output_app.exists() or output_app.is_symlink():
            raise BuildFailure("output path appeared during the build")
        try:
            os.replace(staged_app, output_app)
        except BaseException:
            if previous_app is not None and previous_app.exists():
                os.replace(previous_app, output_app)
            raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return {
        "app": str(output_app),
        "architectures": list(ARCHITECTURES),
        "bundle_identifier": BUNDLE_IDENTIFIER,
        "executable": EXECUTABLE_NAME,
        "minimum_macos": MINIMUM_MACOS,
        "plugin_version": plugin_version(plugin_root),
        "repository_code_executed": False,
        "signature": "ad-hoc" if identity == "-" else "developer-id",
        "team_id": actual_team_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, default=DEFAULT_PLUGIN_ROOT)
    parser.add_argument("--output-app", type=Path, required=True)
    parser.add_argument(
        "--identity",
        default="-",
        help="codesign identity; '-' creates an ad-hoc development artifact",
    )
    parser.add_argument("--expected-team-id")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_app(
            args.plugin_root,
            args.output_app,
            identity=args.identity,
            expected_team_id=args.expected_team_id,
            force=args.force,
        )
    except (BuildFailure, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"EventKit helper build failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
