#!/usr/bin/env python3
"""Independently download and verify one published Apple Reminders release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from audit_source_package import (
    audit_archive,
    audit_source,
    validate_document_mirrors,
)
from build_source_package import build_package
from validate_plugin import validate_root
from verify_runtime_provenance import (
    VerificationError as RuntimeProvenanceError,
    verify_runtime_provenance,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
REPOSITORY = "Oscar-V4/apple-reminders"
CANONICAL_GIT_URL = f"https://github.com/{REPOSITORY}.git"
CANONICAL_MAIN_REF = "refs/remotes/apple-reminders-canonical/release-verifier-main"
RELEASE_WORKFLOW = ".github/workflows/release.yml"
SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
RELEASE_ATTESTATION_V02 = "https://in-toto.io/attestation/release/v0.2"
COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
TAG_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class VerificationError(RuntimeError):
    """A release failed one independent verification gate."""


@dataclass(frozen=True)
class GitIdentity:
    tag: str
    version: str
    tag_commit: str
    tag_object: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    argv: Sequence[str],
    *,
    cwd: Path = REPO_ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        rendered = " ".join(argv)
        raise VerificationError(
            f"command failed with exit code {completed.returncode}: {rendered}\n"
            f"{detail[-8000:]}"
        )
    return completed


def _run_json(argv: Sequence[str], *, cwd: Path = REPO_ROOT) -> Any:
    completed = _run(argv, cwd=cwd)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"command returned malformed JSON: {' '.join(argv)}") from exc


def _require_sha256(value: str, label: str) -> None:
    if not SHA256_RE.fullmatch(value):
        raise VerificationError(f"{label} is not a lowercase SHA-256 digest")


def verify_release_payload(
    root: Path,
    package_name: str,
    *,
    expected_package_sha256: str,
    expected_checksums_sha256: str,
) -> dict[str, str]:
    """Verify the closed two-file payload and its exact checksum statement."""

    _require_sha256(expected_package_sha256, "expected package digest")
    _require_sha256(expected_checksums_sha256, "expected checksum digest")
    expected = {package_name, "SHA256SUMS"}
    if root.is_symlink() or not root.is_dir():
        raise VerificationError("release payload root is missing or unsafe")
    actual = {path.name for path in root.iterdir()}
    if actual != expected:
        raise VerificationError(
            "release payload inventory drift: "
            f"expected={sorted(expected)}, actual={sorted(actual)}"
        )
    for name in sorted(expected):
        path = root / name
        mode = path.lstat().st_mode
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise VerificationError(f"release payload member is unsafe: {name}")
        if stat.S_IMODE(mode) not in {0o600, 0o644}:
            raise VerificationError(f"release payload member mode drift: {name}")

    package_digest = sha256_file(root / package_name)
    checksums_digest = sha256_file(root / "SHA256SUMS")
    if package_digest != expected_package_sha256:
        raise VerificationError("release package digest drift")
    if checksums_digest != expected_checksums_sha256:
        raise VerificationError("release checksum digest drift")
    expected_checksum = f"{package_digest}  {package_name}\n"
    try:
        checksum_text = (root / "SHA256SUMS").read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("checksum file is not UTF-8") from exc
    if checksum_text != expected_checksum:
        raise VerificationError("checksum file drift")
    return {package_name: package_digest, "SHA256SUMS": checksums_digest}


def verify_release_metadata(
    metadata: Any,
    *,
    tag: str,
    tag_commit: str,
    asset_digests: Mapping[str, str],
) -> None:
    """Bind downloaded bytes to one published immutable GitHub Release."""

    if not isinstance(metadata, dict):
        raise VerificationError("release metadata is not an object")
    if metadata.get("isDraft") is not False:
        raise VerificationError("release is still a draft")
    if metadata.get("isImmutable") is not True:
        raise VerificationError("release is not immutable")
    if metadata.get("tagName") != tag:
        raise VerificationError("release tag metadata drift")
    if metadata.get("targetCommitish") != tag_commit:
        raise VerificationError("release target commit drift")
    assets = metadata.get("assets")
    if not isinstance(assets, list) or len(assets) != len(asset_digests):
        raise VerificationError("release asset metadata inventory drift")
    by_name: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise VerificationError("release asset metadata is malformed")
        name = asset["name"]
        if name in by_name:
            raise VerificationError("release asset metadata contains duplicate names")
        by_name[name] = asset
    if set(by_name) != set(asset_digests):
        raise VerificationError("release asset metadata inventory drift")
    for name, expected_digest in asset_digests.items():
        asset = by_name[name]
        if asset.get("digest") != f"sha256:{expected_digest}":
            raise VerificationError(f"release asset metadata digest drift: {name}")
        if asset.get("state") != "uploaded":
            raise VerificationError(f"release asset is not uploaded: {name}")
        size = asset.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise VerificationError(f"release asset size is invalid: {name}")


def _certificate_identity(
    *,
    repository: str,
    workflow: str,
    ref: str,
    commit: str,
    trigger: str,
) -> dict[str, str]:
    identity = f"https://github.com/{repository}/{workflow}@{ref}"
    return {
        "buildConfigDigest": commit,
        "buildConfigURI": identity,
        "buildSignerDigest": commit,
        "buildSignerURI": identity,
        "githubWorkflowRef": ref,
        "githubWorkflowRepository": repository,
        "githubWorkflowSHA": commit,
        "githubWorkflowTrigger": trigger,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryDigest": commit,
        "sourceRepositoryRef": ref,
        "sourceRepositoryURI": f"https://github.com/{repository}",
        "subjectAlternativeName": identity,
    }


def _verify_slsa_item(
    item: Any,
    *,
    repository: str,
    workflow: str,
    ref: str,
    commit: str,
    trigger: str,
    asset_digests: Mapping[str, str],
) -> str:
    if not isinstance(item, dict):
        raise VerificationError("release attestation result is malformed")
    try:
        result = item["verificationResult"]
        certificate = result["signature"]["certificate"]
        statement = result["statement"]
        timestamps = result["verifiedTimestamps"]
    except (KeyError, TypeError) as exc:
        raise VerificationError("release attestation evidence is incomplete") from exc
    if not isinstance(timestamps, list) or not timestamps:
        raise VerificationError("release attestation has no verified timestamp")
    expected_certificate = _certificate_identity(
        repository=repository,
        workflow=workflow,
        ref=ref,
        commit=commit,
        trigger=trigger,
    )
    if not isinstance(certificate, dict):
        raise VerificationError("release attestation certificate is malformed")
    for field, expected in expected_certificate.items():
        if certificate.get(field) != expected:
            raise VerificationError(f"release attestation workflow identity drift: {field}")
    if not isinstance(statement, dict) or statement.get("predicateType") != SLSA_PROVENANCE_V1:
        raise VerificationError("release attestation predicate drift")
    try:
        workflow_parameters = statement["predicate"]["buildDefinition"][
            "externalParameters"
        ]["workflow"]
    except (KeyError, TypeError) as exc:
        raise VerificationError("release attestation workflow evidence is incomplete") from exc
    expected_parameters = {
        "path": workflow,
        "ref": ref,
        "repository": f"https://github.com/{repository}",
    }
    if workflow_parameters != expected_parameters:
        raise VerificationError("release attestation workflow identity drift: parameters")
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or len(subjects) != len(asset_digests):
        raise VerificationError("release attestation subject inventory drift")
    by_name: dict[str, Any] = {}
    for subject in subjects:
        if not isinstance(subject, dict) or not isinstance(subject.get("name"), str):
            raise VerificationError("release attestation subject is malformed")
        name = subject["name"]
        if name in by_name:
            raise VerificationError("release attestation subject inventory drift")
        by_name[name] = subject
    if set(by_name) != set(asset_digests):
        raise VerificationError("release attestation subject inventory drift")
    for name, expected_digest in asset_digests.items():
        if by_name[name].get("digest") != {"sha256": expected_digest}:
            raise VerificationError(f"release attestation subject digest drift: {name}")
    attestation = item.get("attestation")
    if not isinstance(attestation, dict):
        raise VerificationError("release attestation bundle is missing")
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def verify_release_slsa_attestations(
    results_by_subject: Mapping[str, Any],
    *,
    repository: str,
    tag: str,
    tag_commit: str,
    asset_digests: Mapping[str, str],
) -> None:
    """Require both files to resolve to one exact tag-owned SLSA statement."""

    if set(results_by_subject) != set(asset_digests):
        raise VerificationError("release attestation verification inventory drift")
    common_bundles: set[str] | None = None
    for subject_name in sorted(asset_digests):
        results = results_by_subject[subject_name]
        if not isinstance(results, list) or not results:
            raise VerificationError(f"no verified release attestation: {subject_name}")
        bundles: set[str] = set()
        first_error: VerificationError | None = None
        for item in results:
            try:
                bundles.add(
                    _verify_slsa_item(
                        item,
                        repository=repository,
                        workflow=RELEASE_WORKFLOW,
                        ref=f"refs/tags/{tag}",
                        commit=tag_commit,
                        trigger="push",
                        asset_digests=asset_digests,
                    )
                )
            except VerificationError as exc:
                if first_error is None:
                    first_error = exc
        if not bundles:
            if first_error is not None:
                raise first_error
            raise VerificationError(f"no exact release attestation: {subject_name}")
        common_bundles = bundles if common_bundles is None else common_bundles & bundles
    if not common_bundles:
        raise VerificationError("release subjects do not share one attestation bundle")


def verify_release_attestation(
    payload: Any,
    *,
    repository: str,
    tag: str,
    tag_object: str,
    asset_digests: Mapping[str, str],
) -> None:
    """Apply exact tag-object and asset policy after verification succeeds."""

    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict):
        raise VerificationError("release attestation result is malformed")
    try:
        result = payload["verificationResult"]
        certificate = result["signature"]["certificate"]
        statement = result["statement"]
        timestamps = result["verifiedTimestamps"]
    except (KeyError, TypeError) as exc:
        raise VerificationError("release attestation result is incomplete") from exc
    if not isinstance(timestamps, list) or not timestamps:
        raise VerificationError("release attestation has no verified timestamp")
    if not isinstance(certificate, dict) or certificate.get(
        "subjectAlternativeName"
    ) != "https://dotcom.releases.github.com":
        raise VerificationError("release attestation certificate identity drift")
    if not isinstance(statement, dict) or statement.get(
        "predicateType"
    ) != RELEASE_ATTESTATION_V02:
        raise VerificationError("release attestation predicate drift")
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict) or predicate.get("repository") != repository:
        raise VerificationError("release attestation repository drift")
    if predicate.get("tag") != tag:
        raise VerificationError("release attestation tag drift")
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or len(subjects) != len(asset_digests) + 1:
        raise VerificationError("release attestation subject inventory drift")
    expected_uri = f"pkg:github/{repository}@{tag}"
    tag_subjects = [subject for subject in subjects if subject.get("uri") == expected_uri]
    if len(tag_subjects) != 1 or tag_subjects[0].get("digest") != {
        "sha1": tag_object
    }:
        raise VerificationError("release attestation tag subject drift")
    asset_subjects = [subject for subject in subjects if "name" in subject]
    if len(asset_subjects) != len(asset_digests):
        raise VerificationError("release attestation asset inventory drift")
    by_name = {subject.get("name"): subject for subject in asset_subjects}
    if set(by_name) != set(asset_digests):
        raise VerificationError("release attestation asset inventory drift")
    for name, expected_digest in asset_digests.items():
        if by_name[name].get("digest") != {"sha256": expected_digest}:
            raise VerificationError(f"release attestation asset digest drift: {name}")


def _git_output(*argv: str) -> str:
    return _run(("git", *argv)).stdout.strip()


def _require_ancestor(ancestor: str, descendant: str, label: str) -> None:
    completed = _run(
        ("git", "merge-base", "--is-ancestor", ancestor, descendant),
        check=False,
    )
    if completed.returncode != 0:
        raise VerificationError(f"{label} is not an ancestor of the trusted release history")


def resolve_git_identity(tag: str) -> GitIdentity:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise VerificationError("release tag is not strict semantic versioning")
    version = tag.removeprefix("v")
    if Path(_git_output("rev-parse", "--show-toplevel")).resolve() != REPO_ROOT.resolve():
        raise VerificationError("release verifier is not running in its repository")
    if _git_output("status", "--porcelain", "--untracked-files=no"):
        raise VerificationError("tracked worktree must be clean for release verification")
    tag_ref = f"refs/tags/{tag}"
    lines = [
        line.split()
        for line in _git_output(
            "ls-remote", "--refs", CANONICAL_GIT_URL, tag_ref
        ).splitlines()
        if line.strip()
    ]
    if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != tag_ref:
        raise VerificationError("remote release tag identity is missing or ambiguous")
    remote_tag_object = lines[0][0]
    tag_object = _git_output("rev-parse", "--verify", tag_ref)
    tag_commit = _git_output("rev-list", "-n", "1", tag_ref)
    if remote_tag_object != tag_object:
        raise VerificationError("local and remote release tag objects disagree")
    if _git_output("rev-parse", "HEAD") != tag_commit:
        raise VerificationError("checkout is not the exact release tag commit")
    _run(
        (
            "git",
            "fetch",
            "--force",
            "--no-tags",
            CANONICAL_GIT_URL,
            f"+refs/heads/main:{CANONICAL_MAIN_REF}",
        )
    )
    _require_ancestor(
        tag_commit,
        CANONICAL_MAIN_REF,
        "tag commit",
    )
    plugin = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    if plugin.get("version") != version:
        raise VerificationError("release tag and plugin version disagree")
    return GitIdentity(
        tag=tag,
        version=version,
        tag_commit=tag_commit,
        tag_object=tag_object,
    )


def verify_source_and_rebuild(release_root: Path, identity: GitIdentity) -> None:
    validation_errors = validate_root(PLUGIN_ROOT)
    if validation_errors:
        raise VerificationError("plugin validation failed: " + "; ".join(validation_errors))
    source = audit_source(PLUGIN_ROOT, strict_worktree=True)
    mirror_errors = validate_document_mirrors()
    if source.errors or mirror_errors:
        raise VerificationError(
            "source audit failed: " + "; ".join((*source.errors, *mirror_errors))
        )
    package_name = f"apple-reminders-{identity.version}.zip"
    archive_errors = audit_archive(PLUGIN_ROOT, release_root / package_name)
    if archive_errors:
        raise VerificationError("release archive audit failed: " + "; ".join(archive_errors))
    with tempfile.TemporaryDirectory(prefix="apple-reminders-release-rebuild-") as temporary:
        base = Path(temporary)
        build_a = build_package(PLUGIN_ROOT, base / "a")
        build_b = build_package(PLUGIN_ROOT, base / "b")
        downloaded = release_root / package_name
        if build_a.read_bytes() != build_b.read_bytes():
            raise VerificationError("two deterministic rebuilds disagree")
        if build_a.read_bytes() != downloaded.read_bytes():
            raise VerificationError("downloaded release does not match deterministic rebuild")


def _helper_manifest(identity: GitIdentity) -> tuple[Path, dict[str, Any], str, str]:
    manifest_path = PLUGIN_ROOT / "native" / "eventkit-helper-build.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("signed helper manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise VerificationError("signed helper manifest is not an object")
    source_commit = manifest.get("source_commit")
    workflow_commit = manifest.get("workflow_commit")
    if not isinstance(source_commit, str) or not COMMIT_RE.fullmatch(source_commit):
        raise VerificationError("signed helper source commit is invalid")
    if workflow_commit is None and identity.version == "0.5.0":
        workflow_commit = source_commit
    if not isinstance(workflow_commit, str) or not COMMIT_RE.fullmatch(workflow_commit):
        raise VerificationError("signed helper workflow commit is invalid")
    if manifest.get("plugin_version") != identity.version:
        raise VerificationError("signed helper manifest version drift")
    return manifest_path, manifest, source_commit, workflow_commit


def verify_helper_manifest_ancestry_and_attestation(identity: GitIdentity) -> None:
    manifest_path, manifest, source_commit, workflow_commit = _helper_manifest(identity)
    for commit, label in (
        (source_commit, "helper source commit"),
        (workflow_commit, "helper workflow commit"),
    ):
        _run(("git", "cat-file", "-e", f"{commit}^{{commit}}"))
        _require_ancestor(commit, identity.tag_commit, label)
        _require_ancestor(
            commit,
            CANONICAL_MAIN_REF,
            label,
        )
    _require_ancestor(workflow_commit, source_commit, "helper workflow commit")
    legacy = manifest.get("workflow_commit") is None
    workflow = (
        ".github/workflows/prepare-signed-helper.yml"
        if legacy
        else ".github/workflows/prepare-signed-helper-source.yml"
    )
    results = _run_json(
        (
            "gh",
            "attestation",
            "verify",
            str(manifest_path),
            "--repo",
            REPOSITORY,
            "--signer-workflow",
            f"{REPOSITORY}/{workflow}",
            "--signer-digest",
            workflow_commit,
            "--source-digest",
            workflow_commit,
            "--source-ref",
            "refs/heads/main",
            "--predicate-type",
            SLSA_PROVENANCE_V1,
            "--deny-self-hosted-runners",
            "--format",
            "json",
        )
    )
    if not isinstance(results, list) or not results:
        raise VerificationError("no verified signed helper manifest attestation")
    expected_subjects = {
        "AppleRemindersEventKitHelper-notarized.zip",
        "eventkit-helper-build.json",
        "SHA256SUMS",
    }
    manifest_digest = sha256_file(manifest_path)
    first_error: VerificationError | None = None
    for item in results:
        try:
            result = item["verificationResult"]
            certificate = result["signature"]["certificate"]
            statement = result["statement"]
            timestamps = result["verifiedTimestamps"]
            if not isinstance(timestamps, list) or not timestamps:
                raise VerificationError("helper attestation has no verified timestamp")
            expected_certificate = _certificate_identity(
                repository=REPOSITORY,
                workflow=workflow,
                ref="refs/heads/main",
                commit=workflow_commit,
                trigger="workflow_dispatch",
            )
            for field, expected in expected_certificate.items():
                if certificate.get(field) != expected:
                    raise VerificationError(f"helper attestation identity drift: {field}")
            if statement.get("predicateType") != SLSA_PROVENANCE_V1:
                raise VerificationError("helper attestation predicate drift")
            workflow_parameters = statement["predicate"]["buildDefinition"][
                "externalParameters"
            ]["workflow"]
            if workflow_parameters != {
                "path": workflow,
                "ref": "refs/heads/main",
                "repository": f"https://github.com/{REPOSITORY}",
            }:
                raise VerificationError("helper attestation workflow parameters drift")
            subjects = statement.get("subject")
            if not isinstance(subjects, list) or {
                subject.get("name") for subject in subjects if isinstance(subject, dict)
            } != expected_subjects:
                raise VerificationError("helper attestation subject inventory drift")
            by_name = {subject["name"]: subject for subject in subjects}
            if by_name["eventkit-helper-build.json"].get("digest") != {
                "sha256": manifest_digest
            }:
                raise VerificationError("helper manifest attestation digest drift")
            for name in expected_subjects - {"eventkit-helper-build.json"}:
                digest = by_name[name].get("digest")
                if not isinstance(digest, dict) or not SHA256_RE.fullmatch(
                    str(digest.get("sha256", ""))
                ):
                    raise VerificationError("helper attestation subject digest is invalid")
            return
        except (KeyError, TypeError, VerificationError) as exc:
            if isinstance(exc, VerificationError):
                first_error = exc
            else:
                first_error = VerificationError("helper attestation evidence is incomplete")
    raise first_error or VerificationError("no exact helper attestation")


def _release_attestation_with_retries(
    identity: GitIdentity,
    *,
    repository: str,
    attempts: int,
    delay_seconds: float,
) -> Any:
    last_error: VerificationError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _run_json(
                (
                    "gh",
                    "release",
                    "verify",
                    identity.tag,
                    "--repo",
                    repository,
                    "--format",
                    "json",
                )
            )
        except VerificationError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise last_error or VerificationError("release attestation verification failed")


def verify_published_release(
    tag: str,
    *,
    repository: str,
    release_attestation_attempts: int = 1,
    retry_delay_seconds: float = 0,
) -> dict[str, Any]:
    if repository != REPOSITORY:
        raise VerificationError(f"repository must be exactly {REPOSITORY}")
    identity = resolve_git_identity(tag)
    package_name = f"apple-reminders-{identity.version}.zip"
    with tempfile.TemporaryDirectory(prefix="apple-reminders-release-download-") as temporary:
        release_root = Path(temporary)
        _run(
            (
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                repository,
                "--dir",
                str(release_root),
                "--pattern",
                package_name,
                "--pattern",
                "SHA256SUMS",
            )
        )
        provisional_digests = {
            package_name: sha256_file(release_root / package_name),
            "SHA256SUMS": sha256_file(release_root / "SHA256SUMS"),
        }
        asset_digests = verify_release_payload(
            release_root,
            package_name,
            expected_package_sha256=provisional_digests[package_name],
            expected_checksums_sha256=provisional_digests["SHA256SUMS"],
        )
        metadata = _run_json(
            (
                "gh",
                "release",
                "view",
                tag,
                "--repo",
                repository,
                "--json",
                "tagName,targetCommitish,isDraft,isImmutable,assets",
            )
        )
        verify_release_metadata(
            metadata,
            tag=tag,
            tag_commit=identity.tag_commit,
            asset_digests=asset_digests,
        )
        slsa_results = {
            name: _run_json(
                (
                    "gh",
                    "attestation",
                    "verify",
                    str(release_root / name),
                    "--repo",
                    repository,
                    "--signer-workflow",
                    f"{repository}/{RELEASE_WORKFLOW}",
                    "--signer-digest",
                    identity.tag_commit,
                    "--source-digest",
                    identity.tag_commit,
                    "--source-ref",
                    f"refs/tags/{tag}",
                    "--predicate-type",
                    SLSA_PROVENANCE_V1,
                    "--deny-self-hosted-runners",
                    "--format",
                    "json",
                )
            )
            for name in sorted(asset_digests)
        }
        verify_release_slsa_attestations(
            slsa_results,
            repository=repository,
            tag=tag,
            tag_commit=identity.tag_commit,
            asset_digests=asset_digests,
        )
        release_attestation = _release_attestation_with_retries(
            identity,
            repository=repository,
            attempts=release_attestation_attempts,
            delay_seconds=retry_delay_seconds,
        )
        verify_release_attestation(
            release_attestation,
            repository=repository,
            tag=tag,
            tag_object=identity.tag_object,
            asset_digests=asset_digests,
        )
        verify_source_and_rebuild(release_root, identity)
        verify_helper_manifest_ancestry_and_attestation(identity)
        try:
            runtime = verify_runtime_provenance(
                PLUGIN_ROOT / "runtime", identity.tag_commit,
                main_ref=CANONICAL_MAIN_REF,
            )
        except RuntimeProvenanceError as exc:
            raise VerificationError(f"bundled runtime provenance failed: {exc}") from exc
    return {
        "assets": asset_digests,
        "release_attestation": "verified",
        "repository": repository,
        "signed_helper_manifest": "verified",
        "bundled_python_runtime": "verified" if runtime["present"] else "not_in_release",
        "source_audit": "verified",
        "tag": tag,
        "tag_commit": identity.tag_commit,
        "tag_object": identity.tag_object,
        "deterministic_rebuild": "byte-identical",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Exact strict-semver release tag, for example v0.6.0")
    parser.add_argument("--repo", default=REPOSITORY, help="Must name the canonical repository")
    parser.add_argument(
        "--release-attestation-attempts",
        type=int,
        default=1,
        help="Bounded retries for the platform release attestation (default: 1)",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=0,
        help="Delay between release-attestation retries (default: 0)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.release_attestation_attempts <= 12:
        parser.error("--release-attestation-attempts must be between 1 and 12")
    if not 0 <= args.retry_delay_seconds <= 30:
        parser.error("--retry-delay-seconds must be between 0 and 30")
    try:
        result = verify_published_release(
            args.tag,
            repository=args.repo,
            release_attestation_attempts=args.release_attestation_attempts,
            retry_delay_seconds=args.retry_delay_seconds,
        )
    except (OSError, VerificationError, json.JSONDecodeError) as exc:
        parser.exit(1, f"release verification failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
