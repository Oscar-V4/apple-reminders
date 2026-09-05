#!/usr/bin/env python3
"""Bind bundled Python runtime bytes to reviewed Git history and GitHub evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from verify_python_runtime import (
    MAX_ARCHIVE_BYTES,
    SOURCE_INPUTS,
    VerificationError as RuntimeVerificationError,
    load_manifest,
    validate_archive,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Oscar-V4/apple-reminders"
WORKFLOW = ".github/workflows/prepare-signed-runtime-source.yml"
SOURCE_REF = "refs/heads/main"
DEFAULT_MAIN_REF = "refs/remotes/origin/main"
TEAM_ID = "V8347N9346"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ARCHITECTURES = ("arm64", "x86_64")
PAYLOAD_NAMES = frozenset(
    ["SHA256SUMS"]
    + [f"python-runtime-macos-{architecture}.zip" for architecture in ARCHITECTURES]
    + [f"python-runtime-build-{architecture}.json" for architecture in ARCHITECTURES]
)


class VerificationError(RuntimeError):
    """A runtime's bytes, history, or verified attestation did not agree."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(argv: Sequence[str], *, repo_root: Path) -> bytes:
    result = subprocess.run(list(argv), cwd=repo_root, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=180, check=False)
    if result.returncode:
        raise VerificationError(f"runtime provenance command failed: {Path(argv[0]).name} ({result.returncode})")
    if len(result.stdout) > 16 * 1024 * 1024:
        raise VerificationError("runtime provenance command output exceeds bound")
    return result.stdout


def verify_payload(directory: Path) -> dict[str, str]:
    if directory.is_symlink() or not directory.is_dir():
        raise VerificationError("runtime payload directory is missing or unsafe")
    if {path.name for path in directory.iterdir()} != PAYLOAD_NAMES:
        raise VerificationError("runtime payload must contain exactly five release files")
    for name in sorted(PAYLOAD_NAMES):
        path = directory / name
        mode = path.lstat().st_mode
        if path.is_symlink() or not stat.S_ISREG(mode) or stat.S_IMODE(mode) not in {0o600, 0o644}:
            raise VerificationError(f"runtime payload file type or mode is unsafe: {name}")
        bound = 1024 if name == "SHA256SUMS" else (MAX_ARCHIVE_BYTES if name.endswith(".zip") else 4 * 1024 * 1024)
        if not 0 < path.stat().st_size <= bound:
            raise VerificationError(f"runtime payload file size is invalid: {name}")
    digests = {name: sha256_file(directory / name) for name in sorted(PAYLOAD_NAMES)}
    expected = "".join(f"{digests[name]}  {name}\n" for name in sorted(PAYLOAD_NAMES - {"SHA256SUMS"}))
    if (directory / "SHA256SUMS").read_bytes() != expected.encode("ascii"):
        raise VerificationError("runtime checksum file is not the exact four-file statement")
    return digests


def certificate_identity(workflow_commit: str) -> dict[str, str]:
    identity = f"https://github.com/{REPOSITORY}/{WORKFLOW}@{SOURCE_REF}"
    return {
        "buildConfigDigest": workflow_commit,
        "buildConfigURI": identity,
        "buildSignerDigest": workflow_commit,
        "buildSignerURI": identity,
        "githubWorkflowRef": SOURCE_REF,
        "githubWorkflowRepository": REPOSITORY,
        "githubWorkflowSHA": workflow_commit,
        "githubWorkflowTrigger": "workflow_dispatch",
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryDigest": workflow_commit,
        "sourceRepositoryRef": SOURCE_REF,
        "sourceRepositoryURI": f"https://github.com/{REPOSITORY}",
        "subjectAlternativeName": identity,
    }


def verify_attestation_payload(
    payload: Any, *, workflow_commit: str, asset_digests: Mapping[str, str],
) -> None:
    """Apply exact identity and subject policy to successful `gh verify` output."""
    if not isinstance(payload, list) or not payload:
        raise VerificationError("runtime has no verified GitHub attestation")
    first_error: VerificationError | None = None
    for item in payload:
        try:
            if not isinstance(item, dict):
                raise VerificationError("runtime attestation item is malformed")
            result = item["verificationResult"]
            certificate = result["signature"]["certificate"]
            statement = result["statement"]
            timestamps = result["verifiedTimestamps"]
            if not isinstance(timestamps, list) or not timestamps or not all(isinstance(value, dict) and value for value in timestamps):
                raise VerificationError("runtime attestation has no verified timestamp")
            if not isinstance(certificate, dict):
                raise VerificationError("runtime attestation certificate is malformed")
            for field, expected in certificate_identity(workflow_commit).items():
                if certificate.get(field) != expected:
                    raise VerificationError(f"runtime attestation certificate identity drift: {field}")
            if not isinstance(statement, dict) or statement.get("predicateType") != PREDICATE_TYPE:
                raise VerificationError("runtime attestation predicate drift")
            workflow = statement["predicate"]["buildDefinition"]["externalParameters"]["workflow"]
            if workflow != {"path": WORKFLOW, "ref": SOURCE_REF, "repository": f"https://github.com/{REPOSITORY}"}:
                raise VerificationError("runtime attestation external workflow drift")
            subjects = statement.get("subject")
            if not isinstance(subjects, list) or len(subjects) != len(asset_digests) or set(asset_digests) != PAYLOAD_NAMES:
                raise VerificationError("runtime attestation subject inventory drift")
            by_name: dict[str, dict[str, Any]] = {}
            for subject in subjects:
                if not isinstance(subject, dict) or not isinstance(subject.get("name"), str) or subject["name"] in by_name:
                    raise VerificationError("runtime attestation has malformed or duplicate subjects")
                by_name[subject["name"]] = subject
            if set(by_name) != set(asset_digests):
                raise VerificationError("runtime attestation subject inventory drift")
            for name, digest in asset_digests.items():
                if not SHA256.fullmatch(digest) or by_name[name].get("digest") != {"sha256": digest}:
                    raise VerificationError(f"runtime attestation subject digest drift: {name}")
            if not isinstance(item.get("attestation"), dict):
                raise VerificationError("runtime attestation bundle is missing")
            return
        except (KeyError, TypeError, VerificationError) as exc:
            error = exc if isinstance(exc, VerificationError) else VerificationError("runtime attestation evidence is incomplete")
            if first_error is None:
                first_error = error
    raise first_error or VerificationError("runtime has no exact GitHub attestation")


def verify_history(
    manifests: Mapping[str, Mapping[str, Any]], *, tag_commit: str,
    main_ref: str, repo_root: Path,
) -> tuple[str, str]:
    if not isinstance(tag_commit, str) or not COMMIT.fullmatch(tag_commit):
        raise VerificationError("runtime release tag commit must be a full lowercase commit hash")
    if not isinstance(main_ref, str) or not main_ref.startswith("refs/") or any(value in main_ref for value in ("..", "@{", "\\")):
        raise VerificationError("runtime main history reference is invalid")
    _run(["git", "check-ref-format", main_ref], repo_root=repo_root)
    main_commit = _run(["git", "rev-parse", "--verify", f"{main_ref}^{{commit}}"], repo_root=repo_root).decode("ascii").strip()
    if not COMMIT.fullmatch(main_commit):
        raise VerificationError("runtime main history did not resolve to a commit")
    pairs = {(manifest.get("source_commit"), manifest.get("workflow_commit")) for manifest in manifests.values()}
    if len(pairs) != 1:
        raise VerificationError("runtime architectures do not share source and workflow commits")
    source_commit, workflow_commit = pairs.pop()
    if not all(isinstance(value, str) and COMMIT.fullmatch(value) for value in (source_commit, workflow_commit)):
        raise VerificationError("runtime source or workflow commit identity is invalid")
    for commit in {tag_commit, source_commit, workflow_commit, main_commit}:
        if _run(["git", "cat-file", "-t", commit], repo_root=repo_root).strip() != b"commit":
            raise VerificationError("runtime history object is not a commit")
    edges = {
        (source_commit, tag_commit), (source_commit, main_commit),
        (workflow_commit, tag_commit), (workflow_commit, main_commit),
        (workflow_commit, source_commit), (tag_commit, main_commit),
    }
    for ancestor, descendant in sorted(edges):
        try:
            _run(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo_root=repo_root)
        except VerificationError as exc:
            raise VerificationError("runtime provenance commits are outside trusted release history") from exc
    historical: dict[str, str] = {}
    for name in sorted(SOURCE_INPUTS):
        content = _run(["git", "show", f"{source_commit}:{name}"], repo_root=repo_root)
        historical[name] = hashlib.sha256(content).hexdigest()
    for architecture, manifest in manifests.items():
        if manifest.get("source_input_sha256") != historical:
            raise VerificationError(f"runtime historical source input digest drift: {architecture}")
    workflow_content = _run(["git", "show", f"{workflow_commit}:{WORKFLOW}"], repo_root=repo_root)
    if hashlib.sha256(workflow_content).hexdigest() != historical[WORKFLOW]:
        raise VerificationError("runtime workflow changed between signing workflow and source commits")
    return source_commit, workflow_commit


def verify_runtime_provenance(
    runtime_directory: Path, tag_commit: str, main_ref: str = DEFAULT_MAIN_REF,
    *, required: bool = False, repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    if runtime_directory.is_symlink():
        raise VerificationError("runtime payload directory is a symlink")
    if not runtime_directory.exists():
        if required:
            raise VerificationError("required bundled Python runtime is absent")
        return {"present": False, "verified": False}
    digests = verify_payload(runtime_directory)
    manifests: dict[str, dict[str, Any]] = {}
    try:
        for architecture in ARCHITECTURES:
            manifest = load_manifest(runtime_directory / f"python-runtime-build-{architecture}.json", architecture, require_signed=True, expected_team_id=TEAM_ID, repo_root=repo_root)
            validate_archive(runtime_directory / f"python-runtime-macos-{architecture}.zip", manifest)
            manifests[architecture] = manifest
    except (RuntimeVerificationError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"runtime capsule verification failed: {exc}") from exc
    source_commit, workflow_commit = verify_history(manifests, tag_commit=tag_commit, main_ref=main_ref, repo_root=repo_root)
    raw = _run([
        "gh", "attestation", "verify", str(runtime_directory / "SHA256SUMS"),
        "--repo", REPOSITORY, "--signer-workflow", f"{REPOSITORY}/{WORKFLOW}",
        "--signer-digest", workflow_commit, "--source-digest", workflow_commit,
        "--source-ref", SOURCE_REF, "--predicate-type", PREDICATE_TYPE,
        "--deny-self-hosted-runners", "--format", "json",
    ], repo_root=repo_root)
    verify_attestation_payload(json.loads(raw), workflow_commit=workflow_commit, asset_digests=digests)
    return {"present": True, "verified": True, "source_commit": source_commit,
            "workflow_commit": workflow_commit, "asset_sha256": digests}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-directory", type=Path, required=True)
    parser.add_argument("--tag-commit", required=True)
    parser.add_argument("--main-ref", default=DEFAULT_MAIN_REF)
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify_runtime_provenance(args.runtime_directory, args.tag_commit, args.main_ref, required=args.required)
    except (VerificationError, RuntimeVerificationError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Python runtime provenance verification failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
