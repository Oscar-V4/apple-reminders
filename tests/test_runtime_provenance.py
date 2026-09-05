from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import verify_runtime_provenance as provenance
from tests import test_python_runtime_verify as capsule_fixtures

WORKFLOW_COMMIT = "a" * 40
SOURCE_COMMIT = "b" * 40
TAG_COMMIT = "c" * 40
MAIN_COMMIT = "d" * 40


class RuntimeProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = capsule_fixtures.PythonRuntimeVerificationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.repo = fixture.root, fixture.repo
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.manifests: dict[str, dict] = {}
        initial_manifest = copy.deepcopy(fixture.manifest)
        initial_contents = dict(fixture.contents)
        lock = json.loads((self.repo / "scripts/python-runtime-lock.json").read_text())
        for architecture in provenance.ARCHITECTURES:
            fixture.manifest = copy.deepcopy(initial_manifest)
            fixture.contents = dict(initial_contents)
            triple = lock["architectures"][architecture]["target_triple"]
            fixture.manifest.update({"architecture": architecture, "target_triple": triple, "upstream": {kind: lock["architectures"][architecture][kind] for kind in ("install_only_stripped", "full")}})
            metadata = json.loads(fixture.contents["Contents/Resources/upstream/PYTHON.json"])
            metadata["target_triple"] = triple
            metadata["apple_sdk_deployment_target"] = "11.0" if architecture == "arm64" else "10.15"
            fixture.contents["Contents/Resources/upstream/PYTHON.json"] = json.dumps(metadata).encode()
            fixture.archive = self.runtime / f"python-runtime-macos-{architecture}.zip"
            fixture.manifest_path = self.runtime / f"python-runtime-build-{architecture}.json"
            fixture.rebuild()
            fixture.manifest.update({"signature": "developer-id", "team_id": "V8347N9346", "notarized": True, "notarization_checked": True, "source_commit": SOURCE_COMMIT, "workflow_commit": WORKFLOW_COMMIT, "code_directory_hash": "e" * 40, "archive_sha256": fixture.manifest["unsigned_archive_sha256"], "archive_bytes": fixture.manifest["unsigned_archive_bytes"]})
            fixture.save_manifest()
            self.manifests[architecture] = copy.deepcopy(fixture.manifest)
        self.write_checksums()
        self.commands: list[list[str]] = []

    def write_checksums(self) -> None:
        names = sorted(provenance.PAYLOAD_NAMES - {"SHA256SUMS"})
        (self.runtime / "SHA256SUMS").write_text("".join(f"{provenance.sha256_file(self.runtime / name)}  {name}\n" for name in names), encoding="ascii")

    def save_manifest(self, architecture: str) -> None:
        (self.runtime / f"python-runtime-build-{architecture}.json").write_text(json.dumps(self.manifests[architecture]), encoding="utf-8")
        self.write_checksums()

    def attestation(self) -> dict:
        workflow = ".github/workflows/prepare-signed-runtime-source.yml"
        repository = "Oscar-V4/apple-reminders"
        ref = "refs/heads/main"
        identity = f"https://github.com/{repository}/{workflow}@{ref}"
        certificate = {
            "buildConfigDigest": WORKFLOW_COMMIT, "buildConfigURI": identity,
            "buildSignerDigest": WORKFLOW_COMMIT, "buildSignerURI": identity,
            "githubWorkflowRef": ref, "githubWorkflowRepository": repository,
            "githubWorkflowSHA": WORKFLOW_COMMIT, "githubWorkflowTrigger": "workflow_dispatch",
            "runnerEnvironment": "github-hosted", "sourceRepositoryDigest": WORKFLOW_COMMIT,
            "sourceRepositoryRef": ref, "sourceRepositoryURI": f"https://github.com/{repository}",
            "subjectAlternativeName": identity,
        }
        return {
            "attestation": {"bundle": "synthetic verified bundle"},
            "verificationResult": {
                "signature": {"certificate": certificate},
                "verifiedTimestamps": [{"type": "transparency-log"}],
                "statement": {
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "predicate": {"buildDefinition": {"externalParameters": {"workflow": {"path": workflow, "ref": ref, "repository": f"https://github.com/{repository}"}}}},
                    "subject": [{"name": name, "digest": {"sha256": provenance.sha256_file(self.runtime / name)}} for name in sorted(provenance.PAYLOAD_NAMES)],
                },
            },
        }

    def fake_command(self, argv: list[str], *, repo_root: Path) -> bytes:
        self.commands.append(list(argv))
        self.assertEqual(repo_root, self.repo)
        if argv[:3] == ["gh", "attestation", "verify"]:
            return json.dumps([self.attestation()]).encode()
        self.assertEqual(argv[0], "git")
        if argv[1] in {"check-ref-format", "merge-base"}:
            return b""
        if argv[1] == "rev-parse":
            return (MAIN_COMMIT + "\n").encode()
        if argv[1] == "cat-file":
            return b"commit\n"
        if argv[1] == "show":
            commit, name = argv[2].split(":", 1)
            self.assertIn(commit, {SOURCE_COMMIT, WORKFLOW_COMMIT})
            return (self.repo / name).read_bytes()
        self.fail(f"unexpected verification command: {argv}")

    def verify(self) -> dict:
        return provenance.verify_runtime_provenance(self.runtime, TAG_COMMIT, repo_root=self.repo)

    def test_absent_runtime_is_optional_but_required_mode_fails_without_commands(self) -> None:
        missing = self.root / "missing"
        with mock.patch.object(provenance, "_run", side_effect=AssertionError("no external commands for absent runtime")):
            self.assertEqual(provenance.verify_runtime_provenance(missing, TAG_COMMIT), {"present": False, "verified": False})
            with self.assertRaisesRegex(provenance.VerificationError, "absent"):
                provenance.verify_runtime_provenance(missing, TAG_COMMIT, required=True)

    def test_valid_capsules_require_history_and_exact_five_subject_attestation(self) -> None:
        with mock.patch.object(provenance, "_run", side_effect=self.fake_command):
            result = self.verify()
        self.assertTrue(result["verified"])
        self.assertEqual(result["source_commit"], SOURCE_COMMIT)
        self.assertEqual(result["workflow_commit"], WORKFLOW_COMMIT)
        self.assertEqual(set(result["asset_sha256"]), provenance.PAYLOAD_NAMES)
        command = next(item for item in self.commands if item[0] == "gh")
        self.assertEqual(command, [
            "gh", "attestation", "verify", str(self.runtime / "SHA256SUMS"),
            "--repo", "Oscar-V4/apple-reminders", "--signer-workflow", "Oscar-V4/apple-reminders/.github/workflows/prepare-signed-runtime-source.yml",
            "--signer-digest", WORKFLOW_COMMIT, "--source-digest", WORKFLOW_COMMIT,
            "--source-ref", "refs/heads/main", "--predicate-type", "https://slsa.dev/provenance/v1",
            "--deny-self-hosted-runners", "--format", "json",
        ])
        for commit in (SOURCE_COMMIT, WORKFLOW_COMMIT):
            for descendant in (TAG_COMMIT, MAIN_COMMIT):
                self.assertIn(["git", "merge-base", "--is-ancestor", commit, descendant], self.commands)
        self.assertIn(["git", "merge-base", "--is-ancestor", WORKFLOW_COMMIT, SOURCE_COMMIT], self.commands)

    def test_missing_extra_and_symlinked_payload_files_fail_before_external_commands(self) -> None:
        extra = self.runtime / "extra.txt"
        extra.write_text("extra")
        with self.assertRaisesRegex(provenance.VerificationError, "five"):
            provenance.verify_payload(self.runtime)
        extra.unlink()
        path = self.runtime / "python-runtime-macos-arm64.zip"
        content = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(provenance.VerificationError, "five"):
            provenance.verify_payload(self.runtime)
        outside = self.root / "outside.zip"
        outside.write_bytes(content)
        path.symlink_to(outside)
        with self.assertRaisesRegex(provenance.VerificationError, "unsafe"):
            provenance.verify_payload(self.runtime)

    def test_checksum_statement_is_exact_and_covers_all_four_files(self) -> None:
        checksum = self.runtime / "SHA256SUMS"
        original = checksum.read_bytes()
        for replacement in (original + b"\n", b"\n".join(reversed(original.splitlines())) + b"\n", original.replace(b"  ", b" ", 1), original.replace(original[:64], b"0" * 64, 1)):
            with self.subTest(checksum=replacement[:70]):
                checksum.write_bytes(replacement)
                with self.assertRaisesRegex(provenance.VerificationError, "four-file"):
                    provenance.verify_payload(self.runtime)

    def test_unsigned_claims_or_corrupt_capsule_fail_before_git_and_gh(self) -> None:
        self.manifests["arm64"].pop("signature")
        self.save_manifest("arm64")
        with mock.patch.object(provenance, "_run", side_effect=AssertionError("must validate capsule first")):
            with self.assertRaisesRegex(provenance.VerificationError, "capsule verification"):
                self.verify()

    def test_architectures_must_share_both_provenance_commits(self) -> None:
        self.manifests["x86_64"]["source_commit"] = "f" * 40
        self.save_manifest("x86_64")
        with mock.patch.object(provenance, "_run", side_effect=self.fake_command):
            with self.assertRaisesRegex(provenance.VerificationError, "share source"):
                self.verify()
        self.assertFalse(any(item[0] == "gh" for item in self.commands))

    def test_nonancestor_and_noncommit_objects_cannot_reach_attestation(self) -> None:
        for failure in ("ancestry", "object type"):
            with self.subTest(failure=failure):
                def command(argv: list[str], *, repo_root: Path) -> bytes:
                    if failure == "ancestry" and argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                        raise provenance.VerificationError("not an ancestor")
                    if failure == "object type" and argv[:3] == ["git", "cat-file", "-t"]:
                        return b"blob\n"
                    return self.fake_command(argv, repo_root=repo_root)

                with mock.patch.object(provenance, "_run", side_effect=command):
                    with self.assertRaisesRegex(provenance.VerificationError, "trusted release history|not a commit"):
                        self.verify()
        self.assertFalse(any(item[0] == "gh" for item in self.commands))

    def test_historical_source_bytes_and_signing_workflow_must_match(self) -> None:
        for mismatch in ("source", "workflow"):
            with self.subTest(mismatch=mismatch):
                def command(argv: list[str], *, repo_root: Path) -> bytes:
                    if argv[:2] == ["git", "show"]:
                        selected = f"{SOURCE_COMMIT}:scripts/build_python_runtime.py" if mismatch == "source" else f"{WORKFLOW_COMMIT}:{provenance.WORKFLOW}"
                        if argv[2] == selected:
                            return b"different historical content"
                    return self.fake_command(argv, repo_root=repo_root)

                with mock.patch.object(provenance, "_run", side_effect=command):
                    with self.assertRaisesRegex(provenance.VerificationError, "historical source|workflow changed"):
                        self.verify()
        self.assertFalse(any(item[0] == "gh" for item in self.commands))

    def test_all_certificate_identity_fields_are_enforced(self) -> None:
        original = self.attestation()
        digests = provenance.verify_payload(self.runtime)
        for field in original["verificationResult"]["signature"]["certificate"]:
            with self.subTest(field=field):
                item = copy.deepcopy(original)
                item["verificationResult"]["signature"]["certificate"][field] = "wrong identity"
                with self.assertRaisesRegex(provenance.VerificationError, "certificate identity"):
                    provenance.verify_attestation_payload([item], workflow_commit=WORKFLOW_COMMIT, asset_digests=digests)

    def test_attestation_requires_timestamps_exact_workflow_and_predicate(self) -> None:
        original = self.attestation()
        for drift in ("timestamps", "predicate", "workflow", "bundle"):
            with self.subTest(drift=drift):
                item = copy.deepcopy(original)
                result = item["verificationResult"]
                if drift == "timestamps":
                    result["verifiedTimestamps"] = []
                elif drift == "predicate":
                    result["statement"]["predicateType"] = "other predicate"
                elif drift == "workflow":
                    result["statement"]["predicate"]["buildDefinition"]["externalParameters"]["workflow"]["ref"] = "refs/heads/untrusted"
                else:
                    item.pop("attestation")
                with self.assertRaises(provenance.VerificationError):
                    provenance.verify_attestation_payload([item], workflow_commit=WORKFLOW_COMMIT, asset_digests=provenance.verify_payload(self.runtime))

    def test_attestation_must_cover_exact_five_actual_subject_hashes(self) -> None:
        original = self.attestation()
        for drift in ("missing", "extra", "duplicate", "digest"):
            with self.subTest(drift=drift):
                item = copy.deepcopy(original)
                subjects = item["verificationResult"]["statement"]["subject"]
                if drift == "missing":
                    subjects.pop()
                elif drift == "extra":
                    subjects.append({"name": "extra", "digest": {"sha256": "0" * 64}})
                elif drift == "duplicate":
                    subjects[-1] = dict(subjects[0])
                else:
                    subjects[-1]["digest"]["sha256"] = "0" * 64
                with self.assertRaisesRegex(provenance.VerificationError, "subject"):
                    provenance.verify_attestation_payload([item], workflow_commit=WORKFLOW_COMMIT, asset_digests=provenance.verify_payload(self.runtime))

    def test_failed_gh_verification_is_not_replaced_by_manifest_claims(self) -> None:
        def command(argv: list[str], *, repo_root: Path) -> bytes:
            if argv[0] == "gh":
                raise provenance.VerificationError("GitHub verification failed")
            return self.fake_command(argv, repo_root=repo_root)

        with mock.patch.object(provenance, "_run", side_effect=command):
            with self.assertRaisesRegex(provenance.VerificationError, "GitHub verification failed"):
                self.verify()


if __name__ == "__main__":
    unittest.main()
