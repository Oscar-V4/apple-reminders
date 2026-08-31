#!/usr/bin/env python3
"""Fail-closed runtime compatibility model for Experimental Internals.

Stable Core dispatch never consults this model. Experimental operations must
match immutable, repository-reviewed build and schema evidence. There is
deliberately no environment-variable or user-configurable bypass.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import platform
import plistlib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping

from bounded_process import (
    ProcessError,
    run as run_bounded_process,
)


SupportTier = Literal["stable_core", "experimental_internals"]
CompilerRequirement = Literal["not_required", "required", "conditional"]

ATTACHMENT_SCHEMA_FINGERPRINT = (
    "82761d59e465cf4c90ca8c98bb51eab498c6976e81d608023535f3bf0ec63d62"
)
RECOVERY_SCHEMA_FINGERPRINT = (
    "adaa7c550726b35e592085a531fba649466a6099ec8cbb863bf726143fcf5634"
)

SYSTEM_VERSION_PLIST = Path("/System/Library/CoreServices/SystemVersion.plist")
REMINDERS_APP_CANDIDATES = (
    Path("/System/Applications/Reminders.app"),
    Path("/Applications/Reminders.app"),
)
XCODE_SELECT_PATH = Path("/usr/bin/xcode-select")
_SELECTED_CLANG_RELATIVE_PATHS = (
    Path("Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"),
    Path("usr/bin/clang"),
)
_TOOLCHAIN_PROBE_OUTPUT_LIMIT_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    macos_version: str | None
    macos_build: str | None
    reminders_version: str | None
    reminders_build: str | None

    @property
    def complete(self) -> bool:
        return all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                self.macos_version,
                self.macos_build,
                self.reminders_version,
                self.reminders_build,
            )
        )

    @property
    def key(self) -> tuple[str, str, str, str] | None:
        if not self.complete:
            return None
        return (
            str(self.macos_version),
            str(self.macos_build),
            str(self.reminders_version),
            str(self.reminders_build),
        )


@dataclass(frozen=True, slots=True)
class DeveloperToolchainProbe:
    compiler_path: Path | None
    reason_code: str
    selection_attempted: bool

    @property
    def available(self) -> bool:
        return self.compiler_path is not None


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    capability_id: str
    schema_command: str
    compiler_requirement: CompilerRequirement
    mutation: bool


@dataclass(frozen=True, slots=True)
class CompatibilityEvidence:
    evidence_id: str
    identity: RuntimeIdentity
    schema_fingerprints: frozenset[str]


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    capability_id: str
    allowed: bool
    support_tier: SupportTier
    compiler_requirement: CompilerRequirement
    build_compatibility: str
    schema_compatibility: str
    runtime_state: str
    reason_code: str
    evidence_id: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability_id,
            "support_tier": self.support_tier,
            "api_boundary": "private_apple_internals",
            "compiler_requirement": self.compiler_requirement,
            "build_compatibility": self.build_compatibility,
            "schema_compatibility": self.schema_compatibility,
            "runtime_state": self.runtime_state,
            "reason_code": self.reason_code,
            "available": self.allowed,
            "runtime_verification_required": True,
            **(
                {"compatibility_evidence": self.evidence_id}
                if self.evidence_id is not None
                else {}
            ),
        }


CAPABILITY_SPECS: Mapping[str, CapabilitySpec] = MappingProxyType({
    "tag_assignment_mutation": CapabilitySpec(
        "tag_assignment_mutation", "tag_assignment_db", "not_required", True
    ),
    "section_create_mutation": CapabilitySpec(
        "section_create_mutation", "create_section_db", "required", True
    ),
    "section_move_mutation": CapabilitySpec(
        "section_move_mutation", "move_to_section_db", "required", True
    ),
    "legacy_db_image_mutation": CapabilitySpec(
        "legacy_db_image_mutation", "attachment_mutation_db", "not_required", True
    ),
    "image_attachment_mutation": CapabilitySpec(
        "image_attachment_mutation", "attachment_mutation_db", "required", True
    ),
    "url_attachment_mutation": CapabilitySpec(
        "url_attachment_mutation", "attachment_mutation_db", "not_required", True
    ),
    "attachment_delete_mutation": CapabilitySpec(
        "attachment_delete_mutation", "attachment_mutation_db", "conditional", True
    ),
    "recently_deleted_inventory": CapabilitySpec(
        "recently_deleted_inventory", "recover_deleted_reminder", "not_required", False
    ),
    "recently_deleted_exact_inspection": CapabilitySpec(
        "recently_deleted_exact_inspection",
        "recover_deleted_reminder",
        "required",
        False,
    ),
    "recently_deleted_recovery": CapabilitySpec(
        "recently_deleted_recovery", "recover_deleted_reminder", "required", True
    ),
})


_OBSERVED_25F84 = RuntimeIdentity(
    macos_version="26.5.2",
    macos_build="25F84",
    reminders_version="7.0",
    reminders_build="3976",
)
_ATTACHMENT_EVIDENCE = CompatibilityEvidence(
    evidence_id="macos_26_5_2_25f84_reminders_7_0_3976_attachment_schema",
    identity=_OBSERVED_25F84,
    schema_fingerprints=frozenset({ATTACHMENT_SCHEMA_FINGERPRINT}),
)
_RECOVERY_EVIDENCE = CompatibilityEvidence(
    evidence_id="macos_26_5_2_25f84_reminders_7_0_3976_recovery_schema",
    identity=_OBSERVED_25F84,
    schema_fingerprints=frozenset({RECOVERY_SCHEMA_FINGERPRINT}),
)

# Section and tag mutations intentionally have no entries: repository evidence
# does not bind those operations to an exact command-schema fingerprint.  The
# absence is the kill switch, not an invitation to infer compatibility.
COMPATIBILITY_ALLOWLIST: Mapping[
    str, tuple[CompatibilityEvidence, ...]
] = MappingProxyType({
    "image_attachment_mutation": (_ATTACHMENT_EVIDENCE,),
    "url_attachment_mutation": (_ATTACHMENT_EVIDENCE,),
    "attachment_delete_mutation": (_ATTACHMENT_EVIDENCE,),
    "recently_deleted_inventory": (_RECOVERY_EVIDENCE,),
    "recently_deleted_exact_inspection": (_RECOVERY_EVIDENCE,),
    "recently_deleted_recovery": (_RECOVERY_EVIDENCE,),
})


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _read_plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _run_toolchain_probe(argv: list[str], timeout: float) -> Any:
    return run_bounded_process(
        argv,
        timeout_s=timeout,
        stdout_limit=_TOOLCHAIN_PROBE_OUTPUT_LIMIT_BYTES,
        stderr_limit=_TOOLCHAIN_PROBE_OUTPUT_LIMIT_BYTES,
        output="utf8",
    )


def _usable_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_selected_clang(
    *,
    xcode_select: Path = XCODE_SELECT_PATH,
    runner: Callable[[list[str], float], Any] = _run_toolchain_probe,
    selection_tool_usable: Callable[[Path], bool] = _usable_executable,
    compiler_usable: Callable[[Path], bool] = _usable_executable,
    environment: Mapping[str, str] | None = None,
) -> DeveloperToolchainProbe:
    """Resolve clang only from the non-interactive selected developer directory.

    The fixed xcode-select path and fixed compiler-relative paths deliberately
    ignore PATH, /usr/bin/clang shims, and tools that may open an installer.
    """

    effective_environment = os.environ if environment is None else environment
    if any(effective_environment.get(name) for name in ("DEVELOPER_DIR", "TOOLCHAINS")):
        return DeveloperToolchainProbe(None, "developer_environment_override", False)
    if not selection_tool_usable(xcode_select):
        return DeveloperToolchainProbe(None, "xcode_select_unavailable", False)
    try:
        process = runner([str(xcode_select), "-p"], 5.0)
    except (OSError, ProcessError, TimeoutError):
        return DeveloperToolchainProbe(None, "developer_directory_probe_failed", True)
    if getattr(process, "returncode", None) != 0:
        return DeveloperToolchainProbe(None, "developer_directory_unselected", True)
    output = getattr(process, "stdout", "")
    lines = str(output or "").strip().splitlines()
    if len(lines) != 1:
        return DeveloperToolchainProbe(None, "developer_directory_invalid", True)
    developer_dir = Path(lines[0])
    if not developer_dir.is_absolute() or ".." in developer_dir.parts:
        return DeveloperToolchainProbe(None, "developer_directory_invalid", True)
    for relative in _SELECTED_CLANG_RELATIVE_PATHS:
        candidate = developer_dir / relative
        if compiler_usable(candidate):
            return DeveloperToolchainProbe(candidate, "compiler_available", True)
    return DeveloperToolchainProbe(None, "compiler_required", True)


def detect_runtime_identity(
    *,
    system_version_plist: Path = SYSTEM_VERSION_PLIST,
    reminders_candidates: tuple[Path, ...] = REMINDERS_APP_CANDIDATES,
) -> RuntimeIdentity:
    """Read only public bundle/system metadata; never open a Reminders store."""

    system = _read_plist(system_version_plist)
    macos_version = _clean(system.get("ProductUserVisibleVersion")) or _clean(
        system.get("ProductVersion")
    )
    if macos_version is None:
        discovered, _, _ = platform.mac_ver()
        macos_version = _clean(discovered)
    macos_build = _clean(system.get("ProductBuildVersion"))

    reminders: dict[str, Any] = {}
    for candidate in reminders_candidates:
        info = candidate / "Contents/Info.plist"
        if candidate.is_dir() and info.is_file():
            reminders = _read_plist(info)
            break
    return RuntimeIdentity(
        macos_version=macos_version,
        macos_build=macos_build,
        reminders_version=_clean(reminders.get("CFBundleShortVersionString")),
        reminders_build=_clean(reminders.get("CFBundleVersion")),
    )


def runtime_identity_from_diagnostics(
    platform_details: dict[str, Any],
    reminders_details: dict[str, Any],
) -> RuntimeIdentity:
    return RuntimeIdentity(
        macos_version=_clean(platform_details.get("macos_version")),
        macos_build=_clean(platform_details.get("macos_build")),
        reminders_version=_clean(reminders_details.get("version")),
        reminders_build=_clean(reminders_details.get("build")),
    )


def stable_core_capability(
    identity: RuntimeIdentity | None = None,
    *,
    platform_supported: bool = True,
    reminders_available: bool = True,
) -> dict[str, Any]:
    del identity
    available = platform_supported and reminders_available
    reason_code = (
        "documented_eventkit_core"
        if available
        else "unsupported_platform"
        if not platform_supported
        else "reminders_app_missing"
    )
    return {
        "capability": "stable_core",
        "support_tier": "stable_core",
        "api_boundary": "documented_eventkit",
        "compiler_requirement": "not_required",
        "build_compatibility": "not_applicable",
        "schema_compatibility": "not_applicable",
        "runtime_state": "documented_api" if available else "environment_blocked",
        "reason_code": reason_code,
        "available": available,
        "runtime_verification_required": False,
    }


def evaluate_capability(
    capability_id: str,
    identity: RuntimeIdentity,
    *,
    schema_fingerprint: str | None,
    compiler_available: bool,
) -> CapabilityDecision:
    spec = CAPABILITY_SPECS[capability_id]
    evidence = COMPATIBILITY_ALLOWLIST.get(capability_id, ())
    if not identity.complete:
        return CapabilityDecision(
            capability_id,
            False,
            "experimental_internals",
            spec.compiler_requirement,
            "unknown",
            "unverified",
            "runtime_unverified",
            "runtime_unverified",
        )
    if not evidence:
        return CapabilityDecision(
            capability_id,
            False,
            "experimental_internals",
            spec.compiler_requirement,
            "no_evidence",
            "unverified",
            "runtime_unverified",
            "runtime_unverified",
        )
    matched = next((item for item in evidence if item.identity.key == identity.key), None)
    if matched is None:
        return CapabilityDecision(
            capability_id,
            False,
            "experimental_internals",
            spec.compiler_requirement,
            "unsupported",
            "unverified",
            "runtime_unverified",
            "unsupported_build",
        )
    if spec.compiler_requirement == "required" and not compiler_available:
        return CapabilityDecision(
            capability_id,
            False,
            "experimental_internals",
            spec.compiler_requirement,
            "allowlisted",
            "unverified",
            "runtime_unverified",
            "compiler_required",
            matched.evidence_id,
        )
    if schema_fingerprint is None:
        return CapabilityDecision(
            capability_id,
            False,
            "experimental_internals",
            spec.compiler_requirement,
            "allowlisted",
            "unverified",
            "runtime_unverified",
            "schema_unverified",
            matched.evidence_id,
        )
    if schema_fingerprint not in matched.schema_fingerprints:
        return CapabilityDecision(
            capability_id,
            False,
            "experimental_internals",
            spec.compiler_requirement,
            "allowlisted",
            "mismatch",
            "runtime_unverified",
            "schema_fingerprint_mismatch",
            matched.evidence_id,
        )
    return CapabilityDecision(
        capability_id,
        True,
        "experimental_internals",
        spec.compiler_requirement,
        "allowlisted",
        "allowlisted",
        "runtime_unverified",
        "runtime_verification_required",
        matched.evidence_id,
    )


def capability_for_adapter_command(
    command: str,
    *,
    backend: str | None = None,
    image: str | None = None,
    url: str | None = None,
) -> CapabilitySpec | None:
    capability_id: str | None
    if command in {"add_tag", "remove_tag"}:
        capability_id = "tag_assignment_mutation"
    elif command == "create_section":
        capability_id = "section_create_mutation"
    elif command == "move_to_section":
        capability_id = "section_move_mutation"
    elif command == "attach_image":
        capability_id = (
            "legacy_db_image_mutation"
            if backend == "db"
            else "image_attachment_mutation"
        )
    elif command == "copy_image_attachment":
        capability_id = "image_attachment_mutation"
    elif command == "attach_url":
        capability_id = "url_attachment_mutation"
    elif command == "replace_attachment":
        capability_id = (
            "image_attachment_mutation"
            if image
            else "url_attachment_mutation"
            if url
            else None
        )
    elif command == "delete_attachment":
        capability_id = "attachment_delete_mutation"
    elif command == "list_deleted_reminders":
        capability_id = "recently_deleted_inventory"
    elif command == "read_deleted_reminder":
        capability_id = "recently_deleted_exact_inspection"
    elif command == "recover_deleted_reminder":
        capability_id = "recently_deleted_recovery"
    else:
        capability_id = None
    return CAPABILITY_SPECS.get(capability_id) if capability_id else None
