#!/usr/bin/env python3
"""Plan exact EventKit/Native signing dispatches; default is read-only.

Only committed Git blobs are inspected. No fetch, checkout, build, cache update,
helper execution, or credential setup is performed. Use --dispatch explicitly
only after reviewing the plan; the workflows retain their own admission guards.
Keep canonical main frozen until both signing runs finish: GitHub dispatch
selects the existing main branch, not an atomic branch-and-commit pair.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import sys
from typing import Callable, Sequence

from build_eventkit_helper_app import BUILD_INPUT_RELATIVE_PATHS as EVENTKIT_INPUTS
from build_native_helper_app import BUILD_INPUT_RELATIVE_PATHS as NATIVE_INPUTS
from verify_release_assets import CANONICAL_GIT_URL, COMMIT_RE, REPOSITORY, SHA256_RE, TAG_RE

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/apple-reminders/scripts"))
from bounded_process import ProcessError, run  # noqa: E402

MAIN_REF = "refs/heads/main"
WORKFLOWS = ("prepare-signed-helper-source.yml", "prepare-signed-native-helper-source.yml")
BUILD_INPUTS = tuple(sorted(set(EVENTKIT_INPUTS) | set(NATIVE_INPUTS), key=str))
Command = Callable[[Sequence[str], Path], bytes]


class PreflightError(RuntimeError):
    pass


def run_command(argv: Sequence[str], root: Path) -> bytes:
    try:
        result = run(argv, cwd=root, timeout_s=60, stdout_limit=2 * 1024 * 1024,
                     stderr_limit=64 * 1024, output="bytes")
    except (ProcessError, OSError) as exc:
        raise PreflightError(f"{Path(argv[0]).name} command did not complete") from exc
    if result.returncode:
        raise PreflightError(f"{Path(argv[0]).name} command failed ({result.returncode})")
    return result.stdout


def text_command(argv: Sequence[str], root: Path, command: Command) -> str:
    try:
        return command(argv, root).decode("utf-8").strip()
    except UnicodeError as exc:
        raise PreflightError("Git metadata is not UTF-8") from exc


def parse_refs(raw: str) -> dict[str, str]:
    refs = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 2 or not COMMIT_RE.fullmatch(parts[0]) or parts[1] in refs:
            raise PreflightError("Git ref advertisement is invalid or ambiguous")
        refs[parts[1]] = parts[0]
    return refs


def canonical_branch(value: str, refs: dict[str, str], root: Path, command: Command) -> str:
    if (not value or len(value) > 1024 or value in {"HEAD", "@"}
            or value.startswith("-") or (value.startswith("refs/") and not value.startswith("refs/heads/"))):
        raise PreflightError("Select an exact local branch, not a tag, revision, or remote-tracking ref")
    canonical = value if value.startswith("refs/heads/") else f"refs/heads/{value}"
    try:
        command(["git", "check-ref-format", canonical], root)
    except PreflightError as exc:
        raise PreflightError("Invalid source branch ref") from exc
    if canonical not in refs:
        raise PreflightError("The source must exist as an exact local branch")
    if not value.startswith("refs/"):
        candidates = {f"refs/{value}", f"refs/tags/{value}", canonical,
                      f"refs/remotes/{value}", f"refs/remotes/{value}/HEAD"}
        object_matches = (
            text_command(["git", "rev-parse", f"--disambiguate={value}"], root, command)
            if re.fullmatch(r"[0-9a-fA-F]{4,64}", value) else ""
        )
        if set(refs) & candidates != {canonical} or object_matches:
            raise PreflightError("Ambiguous short ref; use the explicit refs/heads/... name")
    return canonical


def plan_signing(source: str, root: Path = ROOT, *, command: Command = run_command) -> dict:
    root = root.expanduser().resolve()
    if Path(text_command(["git", "rev-parse", "--show-toplevel"], root, command)).resolve() != root:
        raise PreflightError("--repo-root must be the repository root")
    local_refs = parse_refs(text_command(
        ["git", "for-each-ref", "--format=%(objectname) %(refname)"], root, command))
    source_ref = canonical_branch(source, local_refs, root, command)
    if MAIN_REF not in local_refs:
        raise PreflightError("A local main branch is required; synchronize it before planning")
    source_commit, main_commit = local_refs[source_ref], local_refs[MAIN_REF]
    for commit in {source_commit, main_commit}:
        if text_command(["git", "cat-file", "-t", commit], root, command) != "commit":
            raise PreflightError("Branch refs must identify commit objects directly")
    raw_manifest = command(["git", "show", f"{source_commit}:plugins/apple-reminders/.codex-plugin/plugin.json"], root)
    if len(raw_manifest) > 65536:
        raise PreflightError("Selected plugin metadata exceeds the size bound")
    try:
        manifest = json.loads(raw_manifest)
        version = manifest["version"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PreflightError("Selected plugin metadata is invalid") from exc
    if not isinstance(version, str) or not TAG_RE.fullmatch(f"v{version}"):
        raise PreflightError("Selected plugin version must be strict semantic versioning")
    tag_ref = f"refs/tags/v{version}"
    if tag_ref in local_refs:
        raise PreflightError("Selected plugin version is already tagged locally")
    try:
        command(["git", "merge-base", "--is-ancestor", main_commit, source_commit], root)
    except PreflightError as exc:
        raise PreflightError("Local main must be an ancestor of the selected source") from exc
    inputs = {}
    for relative in BUILD_INPUTS:
        path = relative.as_posix()
        main_bytes = command(["git", "show", f"{main_commit}:{path}"], root)
        source_bytes = command(["git", "show", f"{source_commit}:{path}"], root)
        if main_bytes != source_bytes:
            raise PreflightError(f"Main-owned signing input changed in selected source: {path}")
        inputs[path] = hashlib.sha256(main_bytes).hexdigest()
    wanted = {MAIN_REF, source_ref, tag_ref}
    remote = parse_refs(text_command(
        ["git", "ls-remote", "--refs", "--heads", "--tags", CANONICAL_GIT_URL, *sorted(wanted)], root, command))
    if set(remote) - wanted:
        raise PreflightError("Canonical remote returned unexpected refs")
    if tag_ref in remote:
        raise PreflightError("Selected plugin version is already tagged on the canonical remote")
    if remote.get(source_ref) != source_commit:
        raise PreflightError("Local and canonical remote source branch commits differ")
    if remote.get(MAIN_REF) != main_commit:
        raise PreflightError("Local and canonical remote main commits differ")
    plan = {"schema_version": 1, "repository": REPOSITORY,
        "repository_root": str(root),
        "preparation_command": [sys.executable, str(Path(__file__).resolve())],
        "source_ref": source_ref, "source_commit": source_commit,
        "workflow_ref": MAIN_REF, "workflow_commit": main_commit,
        "plugin_version": version, "build_inputs": inputs,
        "dispatch_attempted": False,
        "dispatches": [{"workflow": workflow, "argv": [
            "gh", "workflow", "run", workflow, "--repo", REPOSITORY, "--ref", MAIN_REF,
            "--raw-field", f"source_ref={source_ref}",
            "--raw-field", f"source_commit={source_commit}",
        ]} for workflow in WORKFLOWS]}
    plan["plan_sha256"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # Derive the ready-to-run command after hashing its inputs, avoiding a
    # self-referential digest. No additional interactive approval is involved.
    plan["dispatch_argv"] = [*plan["preparation_command"], source_ref,
        "--repo-root", str(root), "--dispatch", "--reviewed-plan-sha256", plan["plan_sha256"]]
    return plan


def dispatch_reviewed_plan(plan: dict, root: Path, *, command: Command = run_command) -> tuple[dict, int]:
    """Revalidate before each request; report partial/uncertain dispatch honestly."""
    root = root.expanduser().resolve()
    receipt = {**plan, "dispatch_results": []}
    for dispatch in plan["dispatches"]:
        try:
            if plan_signing(plan["source_ref"], root, command=command) != plan:
                raise PreflightError("Signing inputs changed after preflight")
        except PreflightError as exc:
            receipt.update(error=str(exc), dispatch_outcome="stopped_before_next_request")
            return receipt, 1
        receipt["dispatch_attempted"] = True
        try:
            command(dispatch["argv"], root)
        except PreflightError:
            receipt["dispatch_results"].append({"workflow": dispatch["workflow"], "outcome": "request_failed_or_unconfirmed"})
            receipt["dispatch_outcome"] = "incomplete_do_not_retry_without_checking_actions"
            return receipt, 1
        receipt["dispatch_results"].append({"workflow": dispatch["workflow"], "outcome": "request_accepted"})
    receipt["dispatch_outcome"] = "requests_accepted"
    return receipt, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_ref", help="Local branch name or exact refs/heads/... ref")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--dispatch", action="store_true", help="Explicitly submit both requests after successful preflight")
    parser.add_argument("--reviewed-plan-sha256", help="Exact plan_sha256 from the reviewed read-only output; required with --dispatch")
    args = parser.parse_args(argv)
    if args.dispatch and not (args.reviewed_plan_sha256 and SHA256_RE.fullmatch(args.reviewed_plan_sha256)):
        parser.error("--dispatch requires the exact --reviewed-plan-sha256")
    if args.reviewed_plan_sha256 and not args.dispatch:
        parser.error("--reviewed-plan-sha256 is used only with --dispatch")
    try:
        plan = plan_signing(args.source_ref, args.repo_root)
        if args.dispatch and args.reviewed_plan_sha256 != plan["plan_sha256"]:
            raise PreflightError("Current plan differs from the reviewed plan; no dispatch was attempted")
        result, status = dispatch_reviewed_plan(plan, args.repo_root) if args.dispatch else (plan, 0)
    except PreflightError as exc:
        parser.exit(1, f"Signing preflight failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
