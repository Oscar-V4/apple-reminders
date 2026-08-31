#!/usr/bin/env python3
"""Audit the deterministic, installable Apple Reminders source package.

Only an explicit runtime allowlist may enter the artifact. Unexpected images,
screenshots, databases, bytecode, archives, backups, caches, journals, and
symlinks are rejected by path before any file content is read.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import plistlib
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from validate_plugin import validate_root


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MIRRORED_ROOT_DOCUMENTS = {
    Path("CHANGELOG.md"),
    Path("LICENSE"),
    Path("PRIVACY.md"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("SUPPORT.md"),
    Path("TERMS.md"),
}
PACKAGE_ROOT_FILES = {
    Path(".codex-plugin/plugin.json"),
    Path(".mcp.json"),
    Path("CHANGELOG.md"),
    Path("LICENSE"),
    Path("PRIVACY.md"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("SUPPORT.md"),
    Path("TERMS.md"),
    Path("assets/icon.png"),
    Path("mcp/server.py"),
    Path("mcp/v2_contract.py"),
    Path("mcp/v2_core.py"),
    Path("mcp/v2_core_backend.py"),
    Path("mcp/v2_diagnostics.py"),
    Path("mcp/v2_native.py"),
    Path("mcp/v2_native_backend.py"),
    Path("mcp/v2_recovery.py"),
    Path("mcp/v2_recovery_backend.py"),
    Path("mcp/v2_transport.py"),
    Path("schemas/mcp-tools.json"),
    Path("scripts/eventkit_bridge.py"),
    Path("scripts/eventkit_bridge_info.plist"),
    Path("scripts/eventkit_protocol.py"),
    Path("scripts/experimental_capabilities.py"),
    Path("scripts/bounded_process.py"),
    Path("scripts/eventkit_bridge_schema.json"),
    Path("scripts/launch_mcp.sh"),
    Path("scripts/durable_idempotency.py"),
    Path("scripts/receipt_contract.py"),
    Path("scripts/reminders_adapter.py"),
    Path("scripts/reminders_image_input.py"),
    Path("scripts/reminders_service.py"),
    Path("scripts/reminders_contracts.py"),
    Path("scripts/reminders_doctor.py"),
    Path("scripts/reminders_eventkit.m"),
    Path("scripts/remkit_attach_image.m"),
    Path("scripts/remkit_recover.m"),
    Path("scripts/remkit_sections.m"),
}
NATIVE_HELPER_APP = Path("native/AppleRemindersEventKitHelper.app")
NATIVE_HELPER_EXECUTABLE = (
    NATIVE_HELPER_APP / "Contents/MacOS/apple-reminders-eventkit-helper"
)
NATIVE_HELPER_MANIFEST = Path("native/eventkit-helper-build.json")
NATIVE_HELPER_FILES = {
    NATIVE_HELPER_APP / "Contents/Info.plist",
    NATIVE_HELPER_EXECUTABLE,
    NATIVE_HELPER_APP / "Contents/_CodeSignature/CodeResources",
    NATIVE_HELPER_APP / "Contents/CodeResources",
    NATIVE_HELPER_MANIFEST,
}
NATIVE_HELPER_OPAQUE_FILES = {
    NATIVE_HELPER_EXECUTABLE,
    NATIVE_HELPER_APP / "Contents/_CodeSignature/CodeResources",
    NATIVE_HELPER_APP / "Contents/CodeResources",
}
NATIVE_HELPER_SOURCE_FILES = {
    ".codex-plugin/plugin.json",
    "scripts/eventkit_bridge_schema.json",
    "scripts/reminders_eventkit.m",
}
NATIVE_HELPER_BUILD_INPUT_FILES = {
    ".github/workflows/prepare-signed-helper-source.yml",
    "scripts/build_eventkit_helper_app.py",
    "scripts/eventkit_helper_app_info.plist",
    "scripts/prepare_signed_eventkit_helper.sh",
    "scripts/verify_eventkit_helper.py",
}
ALLOWED_SKILL_FILES = {
    Path("SKILL.md"),
    Path("agents/openai.yaml"),
    Path("evals/evals.json"),
}
ALLOWED_IMAGE_FILES = {
    Path("assets/icon.png"),
}
FORBIDDEN_FILE_SUFFIXES = {
    ".7z",
    ".bak",
    ".backup",
    ".bmp",
    ".bz2",
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".db3",
    ".dmg",
    ".dump",
    ".gif",
    ".gz",
    ".heic",
    ".jpeg",
    ".jpg",
    ".journal",
    ".jsonl",
    ".log",
    ".m4v",
    ".mov",
    ".mp4",
    ".old",
    ".orig",
    ".pkg",
    ".pyo",
    ".pyc",
    ".rar",
    ".save",
    ".sql",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".swo",
    ".swp",
    ".tar",
    ".tgz",
    ".tif",
    ".tiff",
    ".webp",
    ".xz",
    ".zip",
}
FORBIDDEN_EXACT_NAMES = {
    ".DS_Store",
    "actions.jsonl",
    "cache.json",
    "idempotency.json",
    "verified-capabilities.json",
}
FORBIDDEN_DIRECTORY_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "backups",
    "Container_v1",
}
SCREENSHOT_RE = re.compile(r"(?:screen[ _-]?shot|screenshot|db-first-.*-ui)", re.IGNORECASE)
BACKUP_RE = re.compile(r"(?:^|[._-])backup(?:[._-]|$)|~$")
PRIVATE_HOME_RE = re.compile(r"(?:/Users/|[A-Za-z]:\\Users\\)[^/<\\\s]+[/\\]")
TEXT_SUFFIXES = {".json", ".m", ".md", ".plist", ".py", ".sh", ".yaml", ".yml"}


def package_member_mode(relative: Path) -> int:
    return 0o100755 if relative == NATIVE_HELPER_EXECUTABLE else 0o100644


@dataclass(frozen=True)
class AuditResult:
    files: tuple[Path, ...]
    errors: tuple[str, ...]
    worktree_warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def forbidden_path_reason(relative: Path) -> str | None:
    parts = relative.parts
    if any(part in FORBIDDEN_DIRECTORY_NAMES for part in parts):
        return "generated, backup, cache, or private-data directory"
    if relative.name in FORBIDDEN_EXACT_NAMES:
        return "local metadata or private operational data"
    lower_name = relative.name.casefold()
    if SCREENSHOT_RE.search(relative.as_posix()):
        return "screenshot or UI capture"
    if BACKUP_RE.search(lower_name):
        return "backup file"
    suffixes = {suffix.casefold() for suffix in relative.suffixes}
    forbidden = sorted(suffixes & FORBIDDEN_FILE_SUFFIXES)
    if forbidden:
        return f"forbidden file type {forbidden[-1]}"
    if relative.suffix.casefold() == ".png" and relative not in ALLOWED_IMAGE_FILES:
        return "image outside the reviewed brand asset"
    if lower_name.startswith("reminders-container-backup-"):
        return "Reminders container backup"
    return None


def _allowed_skill_file(relative_to_skill: Path) -> bool:
    if relative_to_skill in ALLOWED_SKILL_FILES:
        return True
    if len(relative_to_skill.parts) == 2:
        folder, name = relative_to_skill.parts
        if folder == "references" and name.endswith(".md"):
            return True
        if folder == "scripts" and name.endswith(".py"):
            return True
    return False


def package_files(root: Path) -> tuple[set[Path], list[str]]:
    """Return the complete runtime package allowlist and policy errors."""

    root = root.expanduser().resolve()
    files = set(PACKAGE_ROOT_FILES)
    errors: list[str] = []
    native_root = root / "native"
    if native_root.exists() or native_root.is_symlink():
        files.update(NATIVE_HELPER_FILES)
    skills_root = root / "skills"
    if not skills_root.is_dir():
        return files, ["missing skills directory"]
    skill_dirs = sorted(path for path in skills_root.iterdir() if path.is_dir())
    if not skill_dirs:
        errors.append("source package must contain at least one skill")
    for skill_dir in skill_dirs:
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            relative_to_skill = path.relative_to(skill_dir)
            relative = path.relative_to(root)
            if forbidden_path_reason(relative):
                # Local caches, captures, archives, and private-data artifacts
                # are reported by the name-only worktree scan below. They are
                # never candidates for the runtime allowlist.
                continue
            if not _allowed_skill_file(relative_to_skill):
                errors.append(f"skill file is outside the runtime allowlist: {relative}")
                continue
            files.add(relative)
        for required in ALLOWED_SKILL_FILES:
            relative = Path("skills") / skill_dir.name / required
            if not (root / relative).is_file():
                errors.append(f"skill runtime file is missing: {relative}")
    return files, errors


def _python_is_stub(path: Path, text: str) -> bool:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return False
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return not body or all(isinstance(node, ast.Pass) for node in body)


def _validate_file(root: Path, relative: Path, errors: list[str]) -> None:
    path = root / relative
    reason = forbidden_path_reason(relative)
    if reason:
        errors.append(f"forbidden package path {relative}: {reason}")
        return
    if not path.exists():
        errors.append(f"required package file is missing: {relative}")
        return
    if path.is_symlink():
        errors.append(f"package symlinks are not allowed: {relative}")
        return
    if not path.is_file():
        errors.append(f"package member is not a regular file: {relative}")
        return
    if path.stat().st_size == 0:
        errors.append(f"empty package stub: {relative}")
        return
    if relative in ALLOWED_IMAGE_FILES:
        return
    if relative in NATIVE_HELPER_OPAQUE_FILES:
        if relative == NATIVE_HELPER_EXECUTABLE:
            if path.read_bytes()[:4] not in {
                b"\xca\xfe\xba\xbe",
                b"\xbe\xba\xfe\xca",
                b"\xca\xfe\xba\xbf",
                b"\xbf\xba\xfe\xca",
            }:
                errors.append(f"native helper is not a universal Mach-O: {relative}")
            if path.stat().st_mode & 0o111 == 0:
                errors.append(f"native helper is not executable: {relative}")
        return
    if path.suffix.casefold() not in TEXT_SUFFIXES and path.name not in {"LICENSE", ".mcp.json"}:
        errors.append(f"unreviewed package file type: {relative}")
        return
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"package text file is unreadable as UTF-8 {relative}: {exc}")
        return
    if not text.strip():
        errors.append(f"whitespace-only package stub: {relative}")
        return
    private_path = PRIVATE_HOME_RE.search(text)
    if private_path:
        # Never echo the matched value: this error is routinely written to CI
        # logs, where repeating a private path would create a second exposure.
        errors.append(f"absolute user-home path found in {relative}")
    if path.suffix == ".py":
        try:
            compile(text, str(relative), "exec")
        except SyntaxError as exc:
            errors.append(f"Python syntax error in {relative}: {exc}")
        if _python_is_stub(relative, text):
            errors.append(f"Python package file appears to be an empty stub: {relative}")
    elif path.suffix == ".json" or path.name == ".mcp.json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid package JSON {relative}: {exc}")
        else:
            if payload in ({}, []):
                errors.append(f"empty JSON package stub: {relative}")
    elif path.suffix == ".plist":
        try:
            payload = plistlib.loads(raw)
        except plistlib.InvalidFileException as exc:
            errors.append(f"invalid package plist {relative}: {exc}")
        else:
            if not isinstance(payload, dict) or not payload:
                errors.append(f"empty plist package stub: {relative}")
    elif path.suffix == ".m" and "int main" not in text:
        errors.append(f"native source lacks an executable entry point: {relative}")


def scan_worktree_for_forbidden(root: Path) -> list[str]:
    """Scan names only; never open a rejected screenshot, archive, DB, or backup."""

    root = root.expanduser().resolve()
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts:
            continue
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(root)
        reason = forbidden_path_reason(relative)
        if reason:
            findings.append(f"{relative}: {reason}")
    return findings


def unallowlisted_runtime_files(root: Path, allowed: set[Path]) -> list[str]:
    """Reject source files a recursive marketplace install would copy."""

    root = root.expanduser().resolve()
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(root)
        if relative in allowed:
            continue
        findings.append(relative.as_posix())
    return findings


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_native_helper_manifest(root: Path) -> list[str]:
    """Bind an optional committed helper to its reviewed source and exact bytes."""

    native_root = root / "native"
    if not native_root.exists() and not native_root.is_symlink():
        return []
    manifest_path = root / NATIVE_HELPER_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return ["native helper manifest is missing or unsafe"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"native helper manifest is invalid: {exc}"]
    if not isinstance(manifest, dict):
        return ["native helper manifest root must be an object"]

    errors: list[str] = []
    expected_top_level_keys = {
        "app_files",
        "app_name",
        "architectures",
        "binary_sha256",
        "build_environment",
        "build_inputs",
        "bundle_identifier",
        "executable",
        "minimum_macos",
        "minimum_macos_by_architecture",
        "notarization_checked",
        "notarized",
        "plugin_version",
        "schema_version",
        "signature",
        "source_commit",
        "source_files",
        "team_id",
        "workflow_commit",
    }
    if set(manifest) != expected_top_level_keys:
        errors.append("native helper manifest top-level key inventory drift")
    missing_source_files = [
        relative
        for relative in sorted(NATIVE_HELPER_SOURCE_FILES)
        if not (root / Path(relative)).is_file()
        or (root / Path(relative)).is_symlink()
    ]
    if missing_source_files:
        errors.append("native helper manifest source inputs are missing or unsafe")
    expected_source_files = {
        relative: _sha256_path(root / Path(relative))
        for relative in sorted(NATIVE_HELPER_SOURCE_FILES)
        if relative not in missing_source_files
    }
    if manifest.get("source_files") != expected_source_files:
        errors.append("native helper manifest source hashes do not match reviewed source")
    missing_build_inputs = [
        relative
        for relative in sorted(NATIVE_HELPER_BUILD_INPUT_FILES)
        if not (REPO_ROOT / relative).is_file()
        or (REPO_ROOT / relative).is_symlink()
    ]
    if missing_build_inputs:
        errors.append("native helper manifest build inputs are missing or unsafe")
    expected_build_inputs = {
        relative: _sha256_path(REPO_ROOT / relative)
        for relative in sorted(NATIVE_HELPER_BUILD_INPUT_FILES)
        if relative not in missing_build_inputs
    }
    if manifest.get("build_inputs") != expected_build_inputs:
        errors.append("native helper manifest build-input hashes do not match reviewed tooling")
    build_environment = manifest.get("build_environment")
    if (
        not isinstance(build_environment, dict)
        or set(build_environment) != {"clang", "linker", "macos_sdk", "xcode_path"}
        or any(
            not isinstance(value, str) or not value.strip()
            for value in build_environment.values()
        )
    ):
        errors.append("native helper manifest build environment provenance is invalid")

    app_members = NATIVE_HELPER_FILES - {NATIVE_HELPER_MANIFEST}
    expected_app_files = {
        relative.relative_to("native").as_posix(): _sha256_path(root / relative)
        for relative in sorted(app_members, key=lambda item: item.as_posix())
        if (root / relative).is_file()
    }
    if manifest.get("app_files") != expected_app_files:
        errors.append("native helper manifest app hashes do not match bundled bytes")
    binary = root / NATIVE_HELPER_EXECUTABLE
    if binary.is_file() and manifest.get("binary_sha256") != _sha256_path(binary):
        errors.append("native helper manifest binary hash does not match")

    plugin_manifest = root / ".codex-plugin" / "plugin.json"
    try:
        plugin_version = json.loads(plugin_manifest.read_text(encoding="utf-8"))["version"]
    except (OSError, KeyError, json.JSONDecodeError):
        plugin_version = None
    expected_metadata = {
        "schema_version": 1,
        "app_name": "AppleRemindersEventKitHelper.app",
        "architectures": ["arm64", "x86_64"],
        "bundle_identifier": "io.github.oscar-v4.apple-reminders.eventkit-bridge",
        "executable": "apple-reminders-eventkit-helper",
        "minimum_macos": "14.0",
        "minimum_macos_by_architecture": {"arm64": "14.0", "x86_64": "14.0"},
        "notarization_checked": True,
        "notarized": True,
        "plugin_version": plugin_version,
        "signature": "developer-id",
        "team_id": "V8347N9346",
    }
    for key, expected in expected_metadata.items():
        if manifest.get(key) != expected:
            errors.append(f"native helper manifest {key} drift")
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}(?:[0-9a-f]{24})?", source_commit
    ):
        errors.append("native helper manifest source_commit is not a full commit hash")
    workflow_commit = manifest.get("workflow_commit")
    if not isinstance(workflow_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}(?:[0-9a-f]{24})?", workflow_commit
    ):
        errors.append("native helper manifest workflow_commit is not a full commit hash")
    return errors


def validate_document_mirrors(
    repo_root: Path = REPO_ROOT,
    plugin_root: Path = PLUGIN_ROOT,
) -> list[str]:
    """Keep install-local public docs byte-identical to canonical GitHub docs."""

    repo_root = repo_root.expanduser().resolve()
    plugin_root = plugin_root.expanduser().resolve()
    errors: list[str] = []
    for relative in sorted(MIRRORED_ROOT_DOCUMENTS, key=lambda path: path.as_posix()):
        canonical = repo_root / relative
        runtime = plugin_root / relative
        if not canonical.is_file():
            errors.append(f"canonical root document is missing: {relative}")
            continue
        if not runtime.is_file():
            errors.append(f"runtime document mirror is missing: {relative}")
            continue
        canonical_digest = hashlib.sha256(canonical.read_bytes()).digest()
        runtime_digest = hashlib.sha256(runtime.read_bytes()).digest()
        if canonical_digest != runtime_digest:
            errors.append(f"runtime document mirror drift: {relative}")
    return errors


def audit_source(root: Path, *, strict_worktree: bool = False) -> AuditResult:
    root = root.expanduser().resolve()
    files, policy_errors = package_files(root)
    errors = list(policy_errors)
    errors.extend(validate_root(root))
    errors.extend(
        f"runtime subtree file is outside the install allowlist: {relative}"
        for relative in unallowlisted_runtime_files(root, files)
    )
    for relative in sorted(files, key=lambda path: path.as_posix()):
        _validate_file(root, relative, errors)
    errors.extend(_validate_native_helper_manifest(root))
    findings = scan_worktree_for_forbidden(root)
    if strict_worktree:
        errors.extend(f"forbidden worktree artifact: {item}" for item in findings)
    return AuditResult(
        files=tuple(sorted(files, key=lambda path: path.as_posix())),
        errors=tuple(dict.fromkeys(errors)),
        worktree_warnings=tuple(findings),
    )


def file_hashes(root: Path, files: Iterable[Path]) -> dict[str, str]:
    return {
        relative.as_posix(): hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in files
    }


def _safe_archive_members(archive: Path) -> tuple[list[zipfile.ZipInfo], list[str]]:
    errors: list[str] = []
    try:
        handle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        return [], [f"invalid source package archive: {exc}"]
    with handle:
        infos = handle.infolist()
    if len({info.filename for info in infos}) != len(infos):
        errors.append("source package contains duplicate member names")
    for info in infos:
        member = PurePosixPath(info.filename)
        if member.is_absolute() or ".." in member.parts or "\\" in info.filename:
            errors.append(f"unsafe archive member path: {info.filename}")
        if info.is_dir():
            errors.append(f"explicit directory entries are not allowed: {info.filename}")
    return infos, errors


def audit_archive(root: Path, archive: Path) -> list[str]:
    """Audit names first, then compare only approved source members byte-for-byte."""

    root = root.expanduser().resolve()
    archive = archive.expanduser().resolve()
    source = audit_source(root)
    errors = list(source.errors)
    if errors:
        return errors
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    package_name = manifest["name"]
    expected_archive_name = f"{package_name}-{manifest['version']}.zip"
    if archive.name != expected_archive_name:
        errors.append(
            f"archive filename/version drift: expected {expected_archive_name}, got {archive.name}"
        )
    infos, name_errors = _safe_archive_members(archive)
    errors.extend(name_errors)
    if errors:
        return errors
    expected = {f"{package_name}/{relative.as_posix()}" for relative in source.files}
    actual = {info.filename for info in infos}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        errors.append(f"archive allowlist mismatch: missing={missing}, extra={extra}")
        return errors
    expected_order = [f"{package_name}/{relative.as_posix()}" for relative in source.files]
    actual_order = [info.filename for info in infos]
    if actual_order != expected_order:
        errors.append("archive members are not in canonical sorted order")
    for info in infos:
        relative = Path(*PurePosixPath(info.filename).parts[1:])
        reason = forbidden_path_reason(relative)
        if reason:
            errors.append(f"forbidden archive member {info.filename}: {reason}")
        if info.date_time != FIXED_ZIP_TIMESTAMP:
            errors.append(f"non-deterministic timestamp on {info.filename}: {info.date_time}")
        if info.compress_type != zipfile.ZIP_STORED:
            errors.append(f"archive member must use deterministic stored mode: {info.filename}")
        expected_mode = package_member_mode(relative)
        if info.create_system != 3 or (info.external_attr >> 16) != expected_mode:
            errors.append(
                "archive member mode drift: "
                f"{info.filename} must be {expected_mode & 0o777:04o}"
            )
    if errors:
        return errors

    # All names are now proven to be allowlisted source. It is safe to compare bytes.
    with zipfile.ZipFile(archive) as handle:
        for relative in source.files:
            member = f"{package_name}/{relative.as_posix()}"
            if handle.read(member) != (root / relative).read_bytes():
                errors.append(f"archive/source content drift: {member}")
    return errors


def _report(result: AuditResult, *, as_json: bool) -> None:
    payload: dict[str, Any] = {
        "ok": result.ok,
        "package_file_count": len(result.files),
        "package_files": [path.as_posix() for path in result.files],
        "errors": list(result.errors),
        "excluded_worktree_artifacts": list(result.worktree_warnings),
        "rejected_artifact_contents_inspected": False,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if result.ok:
        print(f"Source package audit passed: {len(result.files)} allowlisted files")
    for warning in result.worktree_warnings:
        print(f"excluded local artifact (name-only scan): {warning}", file=sys.stderr)
    for error in result.errors:
        print(f"source package audit error: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin", nargs="?", type=Path, default=PLUGIN_ROOT)
    parser.add_argument("--archive", type=Path, help="Audit a deterministic ZIP built from this source")
    parser.add_argument(
        "--strict-worktree",
        action="store_true",
        help="Also fail on ignored local artifacts; normal package audit reports and excludes them.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report")
    parser.add_argument(
        "--verify-root-document-mirrors",
        action="store_true",
        help="Compare install-local public docs with canonical repository-root copies.",
    )
    args = parser.parse_args(argv)
    root = args.plugin.expanduser().resolve()
    if args.archive:
        errors = audit_archive(root, args.archive)
        if args.json:
            print(json.dumps({"ok": not errors, "errors": errors}, indent=2, sort_keys=True))
        elif errors:
            for error in errors:
                print(f"source package archive error: {error}", file=sys.stderr)
        else:
            print(f"Source package archive audit passed: {args.archive}")
        return 1 if errors else 0
    result = audit_source(root, strict_worktree=args.strict_worktree)
    if args.verify_root_document_mirrors:
        mirror_errors = validate_document_mirrors()
        result = AuditResult(
            files=result.files,
            errors=tuple((*result.errors, *mirror_errors)),
            worktree_warnings=result.worktree_warnings,
        )
    _report(result, as_json=args.json)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
