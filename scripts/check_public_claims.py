#!/usr/bin/env python3
"""Detect drift across public beta claims, evidence, and launch copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = Path("plugins/apple-reminders")
MANIFEST = PLUGIN_ROOT / ".codex-plugin/plugin.json"
HELPER_MANIFEST = PLUGIN_ROOT / "native/eventkit-helper-build.json"
LAUNCH_KIT = Path("docs/launch/public-beta-launch-kit.md")
TESTER_WORKFLOW = Path("docs/launch/external-tester-workflow.md")
HISTORICAL_COPY = Path("docs/launch/v0.4.0-ready-copy.md")
RECEIPT_SCHEMA = Path("docs/launch/external-tester-receipt.schema.json")
RECEIPT_EXAMPLE = Path("docs/launch/examples/external-tester-receipt.example.json")
RELEASE_VERIFICATION = Path("docs/release-verification.md")
RELEASE_VERIFIER = Path("scripts/verify_release_assets.py")
RELEASE_WORKFLOW = Path(".github/workflows/release.yml")
EXPERIMENTAL_DECISION = Path(
    "docs/decisions/0020-fail-closed-experimental-runtime-gate.md"
)
SOCIAL_ASSET_README = Path("docs/launch/assets/README.md")
SOCIAL_PREVIEW = Path("docs/launch/assets/apple-reminders-social-preview.png")
SOCIAL_PREVIEW_SHA256 = (
    "91a3f60e194eab13c1dc04a89492d3b13fdc83d24e002a5dc0a94e73c48ed140"
)
SOCIAL_PREVIEW_SOURCE = "9e86c384ce9463df3e97b2cb88441c7341fde033"
MIRRORED_DOCUMENTS = (
    Path("CHANGELOG.md"),
    Path("PRIVACY.md"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("SUPPORT.md"),
)
MARKETING_SURFACES = (
    Path("README.md"),
    Path("PRIVACY.md"),
    Path("SECURITY.md"),
    Path("SUPPORT.md"),
    PLUGIN_ROOT / "README.md",
    PLUGIN_ROOT / "PRIVACY.md",
    PLUGIN_ROOT / "SECURITY.md",
    PLUGIN_ROOT / "SUPPORT.md",
    MANIFEST,
    LAUNCH_KIT,
    TESTER_WORKFLOW,
    SOCIAL_ASSET_README,
    RELEASE_VERIFICATION,
)
FORBIDDEN_CLAIM_RE = re.compile(
    r"\b(?:official|approved|certified|production-ready)\b", re.IGNORECASE
)
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")
RELEASE_TAG_RE = re.compile(
    r"\bv[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\b"
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
BLANKET_XCODE_RE = re.compile(
    r"Python\s+3\.11\+\s*,\s*Xcode(?:\s+Command\s+Line\s+Tools)?",
    re.IGNORECASE,
)


def _read(root: Path, relative: Path, errors: list[str]) -> str:
    try:
        return (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        errors.append(f"{relative.as_posix()}: required public surface is missing")
        return ""


def _load_json(
    root: Path, relative: Path, errors: list[str]
) -> dict[str, Any] | None:
    try:
        payload = json.loads((root / relative).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(f"{relative.as_posix()}: required JSON is invalid")
        return None
    if not isinstance(payload, dict):
        errors.append(f"{relative.as_posix()}: required JSON must be an object")
        return None
    return payload


def _require(
    text: str,
    needle: str,
    relative: Path,
    errors: list[str],
    *,
    label: str | None = None,
) -> None:
    normalized_text = " ".join(text.split())
    normalized_needle = " ".join(needle.split())
    if normalized_needle not in normalized_text:
        errors.append(f"{relative.as_posix()}: missing {label or repr(needle)}")


def _require_all(
    text: str,
    needles: tuple[str, ...],
    relative: Path,
    errors: list[str],
) -> None:
    for needle in needles:
        _require(text, needle, relative, errors)


def check_claims(root: Path = REPO_ROOT) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    texts = {path: _read(root, path, errors) for path in set(MARKETING_SURFACES)}
    changelog_path = Path("CHANGELOG.md")
    changelog = _read(root, changelog_path, errors)
    experimental = _read(root, EXPERIMENTAL_DECISION, errors)
    verifier = _read(root, RELEASE_VERIFIER, errors)
    release_workflow = _read(root, RELEASE_WORKFLOW, errors)

    manifest = _load_json(root, MANIFEST, errors)
    helper = _load_json(root, HELPER_MANIFEST, errors)
    if manifest is None or helper is None:
        return sorted(set(errors))
    try:
        version = manifest["version"]
        card = manifest["interface"]["longDescription"]
        helper_version = helper["plugin_version"]
        helper_source = helper["source_commit"]
        helper_workflow = helper["workflow_commit"]
    except (KeyError, TypeError):
        errors.append("public manifest or helper provenance metadata is incomplete")
        return sorted(set(errors))
    if not all(isinstance(value, str) for value in (version, card)):
        errors.append(f"{MANIFEST.as_posix()}: manifest claim metadata is invalid")
        return sorted(set(errors))
    if helper_version != version:
        errors.append(f"{HELPER_MANIFEST.as_posix()}: plugin version drift")
    for label, value in (("source_commit", helper_source), ("workflow_commit", helper_workflow)):
        if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
            errors.append(f"{HELPER_MANIFEST.as_posix()}: invalid {label}")

    tag = f"v{version}"
    install_command = (
        "codex plugin marketplace add Oscar-V4/apple-reminders --ref " + tag
    )
    add_command = "codex plugin add apple-reminders@oscar-v4-reminders"
    release_verify_command = f"python3 scripts/verify_release_assets.py {tag}"

    for relative in MIRRORED_DOCUMENTS:
        root_text = _read(root, relative, errors)
        plugin_text = _read(root, PLUGIN_ROOT / relative, errors)
        if root_text and plugin_text and root_text != plugin_text:
            errors.append(f"{relative.as_posix()}: install-local document mirror drift")

    for relative in MARKETING_SURFACES:
        if FORBIDDEN_CLAIM_RE.search(texts[relative]):
            errors.append(
                f"{relative.as_posix()}: forbidden readiness or affiliation claim"
            )
        if BLANKET_XCODE_RE.search(texts[relative]):
            errors.append(
                f"{relative.as_posix()}: blanket Xcode requirement hides the Core boundary"
            )

    for relative in (LAUNCH_KIT, TESTER_WORKFLOW):
        text = texts[relative]
        if PLACEHOLDER_RE.search(text):
            errors.append(f"{relative.as_posix()}: unresolved angle-bracket placeholder")
        if "v0.4" in text:
            errors.append(f"{relative.as_posix()}: stale v0.4 launch value")
        _require(text, install_command, relative, errors, label="candidate install command")
        _require(text, add_command, relative, errors, label="plugin add command")
        _require(text, release_verify_command, relative, errors, label="release verifier command")
        _require_all(
            text,
            (
                "macOS 14+",
                "Python 3.11+",
                "Stable Core",
                "Experimental Internals",
                "compiler-free private",
                "CLT-required private",
                "command-schema fingerprint",
                "runtime_unverified",
                "unsupported_build",
                "/usr/bin/xcode-select -p",
                "`PATH`",
                "tool results return to Codex",
                "immutable release",
                "SLSA",
            ),
            relative,
            errors,
        )

    launch = texts[LAUNCH_KIT]
    launch_tags = set(RELEASE_TAG_RE.findall(launch))
    if launch_tags != {tag}:
        errors.append(f"{LAUNCH_KIT.as_posix()}: current release tag drift")
    _require_all(
        launch,
        (
            "release candidate",
            "does not create a tag or GitHub Release",
            "canonical alarm projection",
            "exact read-back",
            str(helper_source),
            str(helper_workflow),
            "60–90 second",
            "Korean SNS draft",
            "English SNS draft",
            SOCIAL_PREVIEW_SHA256,
            SOCIAL_PREVIEW_SOURCE,
            "external-tester-workflow.md",
        ),
        LAUNCH_KIT,
        errors,
    )
    if "public prerelease" in launch:
        errors.append(f"{LAUNCH_KIT.as_posix()}: stale published-release claim")

    readme_path = Path("README.md")
    readme = texts[readme_path]
    _require_all(
        readme,
        (
            install_command,
            "macOS 14 or newer",
            "Python 3.11 or newer",
            "Reminders permission for Core operations",
            "Ordinary Core use does **not** require Xcode or Xcode Command Line Tools",
            "Stable Core",
            "Experimental Internals",
            "compiler-free private",
            "CLT-required private",
            "exact immutable build/schema allowlist match",
            "runtime_unverified",
            "unsupported_build",
            "/usr/bin/xcode-select -p",
            "`PATH` clang entries",
            "/usr/bin/clang",
            "selected developer directory",
            "canonical release verifier",
            "immutable release and SLSA attestations",
        ),
        readme_path,
        errors,
    )

    for needle in (
        "Core runs locally on macOS 14+ without Xcode",
        "exact read-backs",
        "Tag assignments and native URL attachments avoid compilation",
        "version-sensitive private-store features",
        "Section writes, image-attachment changes",
        "require Xcode Command Line Tools",
        "no plugin-owned remote backend",
    ):
        if needle not in card:
            errors.append(f"{MANIFEST.as_posix()}: plugin card missing {needle!r}")

    privacy_path = Path("PRIVACY.md")
    security_path = Path("SECURITY.md")
    support_path = Path("SUPPORT.md")
    _require_all(
        texts[privacy_path],
        (
            "tool results are returned to the Codex host process",
            "prefers public EventKit",
            "undocumented and version-sensitive",
        ),
        privacy_path,
        errors,
    )
    _require_all(
        texts[security_path],
        (
            "Routine fields use EventKit",
            "Stable Core and Experimental Internals are separate runtime support tiers",
            "positive admission for the exact macOS version/build",
            "version-sensitive Apple interfaces",
        ),
        security_path,
        errors,
    )
    _require_all(
        texts[support_path],
        (
            "latest tagged release",
            "metadata-only summary diagnosis",
            "Experimental toolchain diagnosis",
            "minimal, redacted reproduction",
            "Never attach a Reminders database",
        ),
        support_path,
        errors,
    )

    _require_all(
        changelog,
        (
            f"## {version} —",
            "canonical semantic projection",
            "absolute, location, writable relative, or read-only alarms",
            "SLSA provenance",
            "immutable exact macOS/Reminders build and command-schema allowlist",
            "Stable Core",
            "compiler-free private",
            "CLT-required private",
            "xcode-select -p",
        ),
        changelog_path,
        errors,
    )

    _require_all(
        experimental,
        (
            "exact four-part identity",
            "runtime_unverified",
            "unsupported_build",
            "/usr/bin/xcode-select -p",
            "never trusts `PATH` or the `/usr/bin/clang`",
            "schema_fingerprint_mismatch",
            "Stable Core remains independently usable",
        ),
        EXPERIMENTAL_DECISION,
        errors,
    )

    release_doc = texts[RELEASE_VERIFICATION]
    _require_all(
        release_doc,
        (
            "mutable `origin` is never trusted for source identity",
            "gh release verify",
            "gh attestation verify",
            "one shared two-subject statement",
            "immutable releases",
            "deterministic ZIP twice",
            str(helper_source),
            str(helper_workflow),
        ),
        RELEASE_VERIFICATION,
        errors,
    )
    _require_all(
        verifier,
        (
            'REPOSITORY = "Oscar-V4/apple-reminders"',
            "CANONICAL_GIT_URL",
            "CANONICAL_MAIN_REF",
            'RELEASE_WORKFLOW = ".github/workflows/release.yml"',
            "gh",
            "attestation",
            "verify",
            "release",
        ),
        RELEASE_VERIFIER,
        errors,
    )
    _require_all(
        release_workflow,
        (
            "attestations: write",
            "id-token: write",
            "contents: write",
            "gh release create",
            "scripts/verify_release_assets.py",
        ),
        RELEASE_WORKFLOW,
        errors,
    )

    asset_readme = texts[SOCIAL_ASSET_README]
    _require_all(
        asset_readme,
        (
            "1200×630",
            SOCIAL_PREVIEW_SHA256,
            SOCIAL_PREVIEW_SOURCE,
            "Independent · Local MCP · Open source",
        ),
        SOCIAL_ASSET_README,
        errors,
    )
    try:
        preview = (root / SOCIAL_PREVIEW).read_bytes()
    except OSError:
        errors.append(f"{SOCIAL_PREVIEW.as_posix()}: social preview is missing")
    else:
        if hashlib.sha256(preview).hexdigest() != SOCIAL_PREVIEW_SHA256:
            errors.append(f"{SOCIAL_PREVIEW.as_posix()}: social preview digest drift")
        if len(preview) < 24 or preview[:8] != b"\x89PNG\r\n\x1a\n":
            errors.append(f"{SOCIAL_PREVIEW.as_posix()}: social preview is not a PNG")
        else:
            width, height = struct.unpack(">II", preview[16:24])
            if (width, height) != (1200, 630):
                errors.append(f"{SOCIAL_PREVIEW.as_posix()}: social preview dimension drift")

    workflow = texts[TESTER_WORKFLOW]
    for issue in (28, 29, 30, 41):
        _require(
            workflow,
            f"https://github.com/Oscar-V4/apple-reminders/issues/{issue}",
            TESTER_WORKFLOW,
            errors,
        )
    for scenario in (
        "fresh_core_allow",
        "fresh_core_deny",
        "intel_core",
        "minimum_macos_core",
        "upgrade_identity",
        "clt_only_experimental",
    ):
        _require(workflow, f"`{scenario}`", TESTER_WORKFLOW, errors)
    _require_all(
        workflow,
        (
            "python3 scripts/validate_external_tester_receipt.py",
            "release_verification",
            "core_canonical_alarm",
        ),
        TESTER_WORKFLOW,
        errors,
    )

    historical = _read(root, HISTORICAL_COPY, errors)
    _require(
        historical,
        "Historical v0.4.0 record. Do not reuse this as current launch copy",
        HISTORICAL_COPY,
        errors,
        label="historical-copy guard",
    )
    _require(historical, "public-beta-launch-kit.md", HISTORICAL_COPY, errors)

    schema = _load_json(root, RECEIPT_SCHEMA, errors)
    example = _load_json(root, RECEIPT_EXAMPLE, errors)
    if example is not None and example.get("plugin_ref") != tag:
        errors.append(f"{RECEIPT_EXAMPLE.as_posix()}: current release tag drift")
    if schema is not None:
        try:
            checks = schema["properties"]["checks"]["items"]["properties"]
            check_ids = set(checks["id"]["enum"])
            categories = set(checks["error_category"]["enum"])
        except (KeyError, TypeError):
            errors.append(f"{RECEIPT_SCHEMA.as_posix()}: closed checks schema is invalid")
        else:
            if not {"release_verification", "core_canonical_alarm"}.issubset(check_ids):
                errors.append(f"{RECEIPT_SCHEMA.as_posix()}: evidence check drift")
            required_categories = {
                "runtime_unverified",
                "unsupported_build",
                "compiler_required",
                "schema_unverified",
                "schema_fingerprint_mismatch",
                "unsupported_capability",
                "schema_mismatch",
            }
            if not required_categories.issubset(categories):
                errors.append(f"{RECEIPT_SCHEMA.as_posix()}: error category drift")

    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    errors = check_claims(args.root)
    if errors:
        for error in errors:
            print(f"public claim error: {error}", file=sys.stderr)
        return 1
    print("Public beta claim check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
