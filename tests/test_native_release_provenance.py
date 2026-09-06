from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_release_assets as release
import verify_native_helper as native
from build_native_helper_app import BUILD_INPUT_RELATIVE_PATHS, SOURCES

SOURCE = "a" * 40
WORKFLOW = "b" * 40
TAG_COMMIT = "c" * 40
DIGEST = "d" * 64
IDENTITY = release.GitIdentity("v1.2.3", "1.2.3", TAG_COMMIT, "e" * 40)
HISTORICAL_SHA256 = release._historical_sha256


def attestation(manifest_digest: str = DIGEST) -> dict:
    return {
        "attestation": {"bundle": "synthetic-verified-bundle"},
        "verificationResult": {
            "verifiedTimestamps": [{"type": "transparency-log"}],
            "signature": {"certificate": release._certificate_identity(
                repository=release.REPOSITORY, workflow=release.NATIVE_WORKFLOW,
                ref="refs/heads/main", commit=WORKFLOW, trigger="workflow_dispatch",
            )},
            "statement": {
                "predicateType": release.SLSA_PROVENANCE_V1,
                "predicate": {"buildDefinition": {"externalParameters": {"workflow": {
                    "path": release.NATIVE_WORKFLOW, "ref": "refs/heads/main",
                    "repository": f"https://github.com/{release.REPOSITORY}",
                }}}},
                "subject": [
                    {"name": name, "digest": {"sha256": manifest_digest if name == "native-helper-build.json" else "f" * 64}}
                    for name in sorted(release.NATIVE_SUBJECTS)
                ],
            },
        },
    }


class NativeAttestationTests(unittest.TestCase):
    def verify(self, item: dict) -> None:
        release.verify_native_manifest_attestation([item], workflow_commit=WORKFLOW, manifest_digest=DIGEST)

    def test_exact_three_subject_main_owned_statement_is_accepted(self) -> None:
        self.verify(attestation())

    def test_each_certificate_identity_field_is_bound(self) -> None:
        good = attestation()
        for field in good["verificationResult"]["signature"]["certificate"]:
            with self.subTest(field=field):
                changed = deepcopy(good)
                changed["verificationResult"]["signature"]["certificate"][field] = "untrusted"
                with self.assertRaises(release.VerificationError):
                    self.verify(changed)

    def test_closed_subjects_digest_and_workflow_evidence(self) -> None:
        def subjects(item):
            return item["verificationResult"]["statement"]["subject"]
        mutations = (
            lambda item: subjects(item).append(deepcopy(subjects(item)[0])),
            lambda item: subjects(item).pop(),
            lambda item: subjects(item)[0].update(name="unexpected.zip"),
            lambda item: subjects(item)[0]["digest"].update(sha512="0" * 128),
            lambda item: subjects(item)[0]["digest"].update(sha256="bad"),
            lambda item: next(subject for subject in subjects(item) if subject["name"] == "native-helper-build.json")["digest"].update(sha256="0" * 64),
            lambda item: item["verificationResult"].update(verifiedTimestamps=[]),
            lambda item: item["verificationResult"]["statement"].update(predicateType="unexpected"),
            lambda item: item["verificationResult"]["statement"]["predicate"]["buildDefinition"]["externalParameters"]["workflow"].update(ref="refs/heads/unreviewed"),
            lambda item: item.pop("attestation"),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                item = attestation()
                mutate(item)
                with self.assertRaises(release.VerificationError):
                    self.verify(item)
        # Duplicate one subject while retaining the expected total length.
        item = attestation()
        subjects(item)[0] = deepcopy(subjects(item)[1])
        with self.assertRaises(release.VerificationError):
            self.verify(item)


class NativeReleasePairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.plugin = self.root / "plugins/apple-reminders"
        self.native_dir = self.plugin / "native"
        self.native_dir.mkdir(parents=True)
        self.app = self.native_dir / "AppleRemindersNativeHelper.app"
        self.manifest_path = self.native_dir / "native-helper-build.json"
        self.stack.enter_context(mock.patch.object(release, "PLUGIN_ROOT", self.plugin))
        self.run = self.stack.enter_context(mock.patch.object(release, "_run"))
        self.ancestor = self.stack.enter_context(mock.patch.object(release, "_require_ancestor"))
        self.historical = self.stack.enter_context(mock.patch.object(release, "_historical_sha256", return_value=DIGEST))
        self.attest = self.stack.enter_context(mock.patch.object(release, "_run_json"))
        self.verify_app = self.stack.enter_context(mock.patch.object(native, "verify_app", return_value={"app_files": {"synthetic": DIGEST}}))
        self.verify_manifest = self.stack.enter_context(mock.patch.object(native, "verify_manifest"))

    def make_pair(self) -> dict:
        self.app.mkdir()
        manifest = {
            "schema_version": 1, "plugin_version": IDENTITY.version,
            "source_commit": SOURCE, "workflow_commit": WORKFLOW,
            "source_files": {path.as_posix(): DIGEST for path in SOURCES.values()},
            "build_inputs": {path.as_posix(): DIGEST for path in BUILD_INPUT_RELATIVE_PATHS},
        }
        self.manifest_path.write_text(json.dumps(manifest))
        self.attest.return_value = [attestation(release.sha256_file(self.manifest_path))]
        return manifest

    def test_absent_pair_preserves_historical_release_without_probes(self) -> None:
        for version in ("0.5.0", "0.6.1", "0.6.99"):
            with self.subTest(version=version):
                identity = release.GitIdentity(f"v{version}", version, TAG_COMMIT, "e" * 40)
                self.assertEqual(release.verify_native_helper_provenance(identity), {"present": False})
        for probe in (self.run, self.ancestor, self.historical, self.attest, self.verify_app):
            probe.assert_not_called()

    def test_modern_release_requires_both_assets_before_any_probe(self) -> None:
        for version in ("0.7.0", "0.7.1", "0.10.0", "1.0.0", "10.0.0"):
            with self.subTest(version=version):
                identity = release.GitIdentity(f"v{version}", version, TAG_COMMIT, "e" * 40)
                with self.assertRaisesRegex(release.VerificationError, "required for releases 0.7.0 onward"):
                    release.verify_native_helper_provenance(identity)
        for probe in (self.run, self.ancestor, self.historical, self.attest, self.verify_app):
            probe.assert_not_called()

    def test_invalid_version_cannot_select_the_legacy_optional_policy(self) -> None:
        for version in ("0.07.0", "0.7", "0.7.0-beta", ""):
            identity = release.GitIdentity(f"v{version}", version, TAG_COMMIT, "e" * 40)
            with self.subTest(version=version), self.assertRaisesRegex(release.VerificationError, "semantic versioning"):
                release.verify_native_helper_provenance(identity)

    def test_partial_pair_and_symlinks_are_rejected_before_network(self) -> None:
        for kind in ("app_only", "manifest_only", "symlink"):
            with self.subTest(kind=kind):
                if kind == "app_only":
                    self.app.mkdir()
                elif kind == "manifest_only":
                    self.manifest_path.write_text("{}")
                else:
                    self.app.symlink_to(self.root / "missing")
                    self.manifest_path.write_text("{}")
                for version in ("0.6.1", "0.7.0"):
                    identity = release.GitIdentity(f"v{version}", version, TAG_COMMIT, "e" * 40)
                    with self.subTest(version=version), self.assertRaisesRegex(
                        release.VerificationError, "complete regular pair"
                    ):
                        release.verify_native_helper_provenance(identity)
                self.attest.assert_not_called()
                self.run.assert_not_called()
                if self.app.is_symlink():
                    self.app.unlink()
                elif self.app.exists():
                    self.app.rmdir()
                self.manifest_path.unlink(missing_ok=True)

    def test_complete_pair_binds_history_attestation_and_structural_manifest(self) -> None:
        self.make_pair()
        result = release.verify_native_helper_provenance(IDENTITY)
        self.assertTrue(result["present"])
        self.assertEqual(self.ancestor.call_args_list, [
            mock.call(SOURCE, TAG_COMMIT, "Native source commit"),
            mock.call(SOURCE, release.CANONICAL_MAIN_REF, "Native source commit"),
            mock.call(WORKFLOW, TAG_COMMIT, "Native workflow commit"),
            mock.call(WORKFLOW, release.CANONICAL_MAIN_REF, "Native workflow commit"),
            mock.call(TAG_COMMIT, release.CANONICAL_MAIN_REF, "Native release commit"),
            mock.call(WORKFLOW, SOURCE, "Native workflow commit"),
        ])
        for path in BUILD_INPUT_RELATIVE_PATHS:
            self.historical.assert_any_call(SOURCE, path.as_posix())
            self.historical.assert_any_call(WORKFLOW, path.as_posix())
        argv = self.attest.call_args.args[0]
        for flag, value in (("--repo", release.REPOSITORY), ("--signer-digest", WORKFLOW), ("--source-digest", WORKFLOW), ("--source-ref", "refs/heads/main")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--deny-self-hosted-runners", argv)
        self.verify_app.assert_called_once_with(self.plugin, self.app, expected_team_id="V8347N9346", require_developer_id=True, require_notarized=True)
        self.verify_manifest.assert_called_once_with(self.plugin, self.manifest_path, self.verify_app.return_value, expected_source_commit=SOURCE, expected_workflow_commit=WORKFLOW)

    def test_release_workflow_main_ref_keeps_all_ancestry_gates(self) -> None:
        self.make_pair()
        main_ref = "refs/remotes/origin/main"
        release.verify_native_helper_provenance(IDENTITY, main_ref=main_ref)
        self.assertEqual(self.ancestor.call_args_list, [
            mock.call(SOURCE, TAG_COMMIT, "Native source commit"),
            mock.call(SOURCE, main_ref, "Native source commit"),
            mock.call(WORKFLOW, TAG_COMMIT, "Native workflow commit"),
            mock.call(WORKFLOW, main_ref, "Native workflow commit"),
            mock.call(TAG_COMMIT, main_ref, "Native release commit"),
            mock.call(WORKFLOW, SOURCE, "Native workflow commit"),
        ])

    def test_version_source_build_input_and_history_drift_fail_closed(self) -> None:
        manifest = self.make_pair()
        for field in ("plugin_version", "source_commit", "workflow_commit", "source_files", "build_inputs", "schema_version"):
            with self.subTest(field=field):
                changed = deepcopy(manifest)
                changed[field] = "wrong"
                self.manifest_path.write_text(json.dumps(changed))
                with self.assertRaises(release.VerificationError):
                    release.verify_native_helper_provenance(IDENTITY)
                self.attest.assert_not_called()
        self.manifest_path.write_text(json.dumps(manifest))
        self.ancestor.side_effect = release.VerificationError("outside trusted release history")
        with self.assertRaisesRegex(release.VerificationError, "trusted release history"):
            release.verify_native_helper_provenance(IDENTITY)
        self.attest.assert_not_called()

    def test_workflow_source_tooling_difference_fails_before_attestation(self) -> None:
        self.make_pair()
        self.historical.side_effect = lambda commit, path: "0" * 64 if commit == WORKFLOW else DIGEST
        with self.assertRaisesRegex(release.VerificationError, "tooling changed"):
            release.verify_native_helper_provenance(IDENTITY)
        self.attest.assert_not_called()

    def test_expanded_byte_or_signature_failure_cannot_be_authenticated_by_manifest_alone(self) -> None:
        self.make_pair()
        self.verify_manifest.side_effect = native.BuildFailure("app byte mismatch")
        with self.assertRaisesRegex(release.VerificationError, "app/manifest verification failed"):
            release.verify_native_helper_provenance(IDENTITY)

    def test_historical_hash_preserves_exact_binary_bytes(self) -> None:
        raw = b"text\r\nnon-utf8:\xff"
        with mock.patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, raw, b"")) as run:
            self.assertEqual(HISTORICAL_SHA256(SOURCE, "path"), hashlib.sha256(raw).hexdigest())
        self.assertNotIn("text", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
