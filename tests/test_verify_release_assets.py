from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
VERIFICATION_DOC = ROOT / "docs" / "release-verification.md"
sys.path.insert(0, str(SCRIPTS))

import verify_release_assets  # noqa: E402


TAG = "v1.2.3"
VERSION = "1.2.3"
COMMIT = "a" * 40
TAG_OBJECT = "d" * 40
PACKAGE = f"apple-reminders-{VERSION}.zip"
PACKAGE_DIGEST = "b" * 64
CHECKSUMS_DIGEST = "c" * 64
REPOSITORY = "Oscar-V4/apple-reminders"


def certificate() -> dict[str, str]:
    identity = (
        "https://github.com/Oscar-V4/apple-reminders/.github/workflows/"
        f"release.yml@refs/tags/{TAG}"
    )
    return {
        "buildConfigDigest": COMMIT,
        "buildConfigURI": identity,
        "buildSignerDigest": COMMIT,
        "buildSignerURI": identity,
        "githubWorkflowRef": f"refs/tags/{TAG}",
        "githubWorkflowRepository": REPOSITORY,
        "githubWorkflowSHA": COMMIT,
        "githubWorkflowTrigger": "push",
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryDigest": COMMIT,
        "sourceRepositoryRef": f"refs/tags/{TAG}",
        "sourceRepositoryURI": f"https://github.com/{REPOSITORY}",
        "subjectAlternativeName": identity,
    }


def slsa_item(*, checksum_digest: str = CHECKSUMS_DIGEST) -> dict:
    statement = {
        "predicateType": verify_release_assets.SLSA_PROVENANCE_V1,
        "predicate": {
            "buildDefinition": {
                "externalParameters": {
                    "workflow": {
                        "path": ".github/workflows/release.yml",
                        "ref": f"refs/tags/{TAG}",
                        "repository": f"https://github.com/{REPOSITORY}",
                    }
                }
            }
        },
        "subject": [
            {"name": PACKAGE, "digest": {"sha256": PACKAGE_DIGEST}},
            {"name": "SHA256SUMS", "digest": {"sha256": checksum_digest}},
        ],
    }
    return {
        "attestation": {"bundle": "same-signed-bundle"},
        "verificationResult": {
            "signature": {"certificate": certificate()},
            "statement": statement,
            "verifiedTimestamps": [{"type": "transparency-log"}],
        },
    }


class ReleasePayloadTests(unittest.TestCase):
    def write_payload(self, root: Path) -> tuple[str, str]:
        package = root / PACKAGE
        package.write_bytes(b"deterministic-package")
        package_digest = hashlib.sha256(package.read_bytes()).hexdigest()
        checksums = root / "SHA256SUMS"
        checksums.write_text(f"{package_digest}  {PACKAGE}\n", encoding="utf-8")
        return package_digest, hashlib.sha256(checksums.read_bytes()).hexdigest()

    def test_exact_payload_inventory_checksum_and_modes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_digest, checksums_digest = self.write_payload(root)

            verified = verify_release_assets.verify_release_payload(
                root,
                PACKAGE,
                expected_package_sha256=package_digest,
                expected_checksums_sha256=checksums_digest,
            )

            self.assertEqual(
                verified,
                {PACKAGE: package_digest, "SHA256SUMS": checksums_digest},
            )

    def test_extra_member_and_checksum_self_reference_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_digest, checksums_digest = self.write_payload(root)
            (root / "extra.txt").write_text("drift", encoding="utf-8")
            with self.assertRaisesRegex(
                verify_release_assets.VerificationError,
                "inventory drift",
            ):
                verify_release_assets.verify_release_payload(
                    root,
                    PACKAGE,
                    expected_package_sha256=package_digest,
                    expected_checksums_sha256=checksums_digest,
                )

            (root / "extra.txt").unlink()
            checksums = root / "SHA256SUMS"
            checksums.write_text(
                checksums.read_text(encoding="utf-8")
                + f"{'d' * 64}  SHA256SUMS\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                verify_release_assets.VerificationError,
                "checksum file drift",
            ):
                verify_release_assets.verify_release_payload(
                    root,
                    PACKAGE,
                    expected_package_sha256=package_digest,
                    expected_checksums_sha256=hashlib.sha256(
                        checksums.read_bytes()
                    ).hexdigest(),
                )

    def test_symlink_and_executable_mode_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_digest, checksums_digest = self.write_payload(root)
            package = root / PACKAGE
            package.chmod(0o755)
            with self.assertRaisesRegex(
                verify_release_assets.VerificationError,
                "mode drift",
            ):
                verify_release_assets.verify_release_payload(
                    root,
                    PACKAGE,
                    expected_package_sha256=package_digest,
                    expected_checksums_sha256=checksums_digest,
                )
            package.chmod(0o644)
            target = root / "package-target"
            package.rename(target)
            package.symlink_to(target.name)
            target.unlink()
            with self.assertRaisesRegex(
                verify_release_assets.VerificationError,
                "unsafe",
            ):
                verify_release_assets.verify_release_payload(
                    root,
                    PACKAGE,
                    expected_package_sha256=package_digest,
                    expected_checksums_sha256=checksums_digest,
                )


class GitIdentityTests(unittest.TestCase):
    def test_identity_network_lookups_ignore_origin_and_use_canonical_git_url(
        self,
    ) -> None:
        tag_ref = f"refs/tags/{TAG}"
        git_calls: list[tuple[str, ...]] = []

        def fake_git_output(*argv: str) -> str:
            git_calls.append(argv)
            responses = {
                ("rev-parse", "--show-toplevel"): str(ROOT),
                ("status", "--porcelain", "--untracked-files=no"): "",
                (
                    "ls-remote",
                    "--refs",
                    verify_release_assets.CANONICAL_GIT_URL,
                    tag_ref,
                ): f"{TAG_OBJECT}\t{tag_ref}",
                ("rev-parse", "--verify", tag_ref): TAG_OBJECT,
                ("rev-list", "-n", "1", tag_ref): COMMIT,
                ("rev-parse", "HEAD"): COMMIT,
            }
            try:
                return responses[argv]
            except KeyError as exc:
                raise AssertionError(f"unexpected git lookup: {argv}") from exc

        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary)
            metadata = plugin_root / ".codex-plugin" / "plugin.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({"version": VERSION}), encoding="utf-8")
            with (
                mock.patch.object(
                    verify_release_assets,
                    "_git_output",
                    side_effect=fake_git_output,
                ),
                mock.patch.object(verify_release_assets, "_run") as run,
                mock.patch.object(verify_release_assets, "_require_ancestor") as ancestor,
                mock.patch.object(verify_release_assets, "PLUGIN_ROOT", plugin_root),
            ):
                identity = verify_release_assets.resolve_git_identity(TAG)

        self.assertEqual(identity.tag_object, TAG_OBJECT)
        self.assertIn(
            (
                "ls-remote",
                "--refs",
                verify_release_assets.CANONICAL_GIT_URL,
                tag_ref,
            ),
            git_calls,
        )
        run.assert_called_once_with(
            (
                "git",
                "fetch",
                "--force",
                "--no-tags",
                verify_release_assets.CANONICAL_GIT_URL,
                "+refs/heads/main:"
                f"{verify_release_assets.CANONICAL_MAIN_REF}",
            )
        )
        ancestor.assert_called_once_with(
            COMMIT,
            verify_release_assets.CANONICAL_MAIN_REF,
            "tag commit",
        )
        self.assertNotIn("origin", " ".join(" ".join(call) for call in git_calls))


class AttestationPolicyTests(unittest.TestCase):
    def test_slsa_results_require_one_common_exact_two_subject_statement(self) -> None:
        results = {PACKAGE: [slsa_item()], "SHA256SUMS": [slsa_item()]}

        verify_release_assets.verify_release_slsa_attestations(
            results,
            repository=REPOSITORY,
            tag=TAG,
            tag_commit=COMMIT,
            asset_digests={
                PACKAGE: PACKAGE_DIGEST,
                "SHA256SUMS": CHECKSUMS_DIGEST,
            },
        )

    def test_slsa_subject_digest_or_workflow_identity_drift_fails(self) -> None:
        bad_digest = {
            PACKAGE: [slsa_item(checksum_digest="d" * 64)],
            "SHA256SUMS": [slsa_item(checksum_digest="d" * 64)],
        }
        with self.assertRaisesRegex(
            verify_release_assets.VerificationError,
            "subject digest drift",
        ):
            verify_release_assets.verify_release_slsa_attestations(
                bad_digest,
                repository=REPOSITORY,
                tag=TAG,
                tag_commit=COMMIT,
                asset_digests={
                    PACKAGE: PACKAGE_DIGEST,
                    "SHA256SUMS": CHECKSUMS_DIGEST,
                },
            )

        bad_identity_item = slsa_item()
        bad_identity_item["verificationResult"]["signature"]["certificate"][
            "githubWorkflowRef"
        ] = "refs/heads/main"
        with self.assertRaisesRegex(
            verify_release_assets.VerificationError,
            "workflow identity drift",
        ):
            verify_release_assets.verify_release_slsa_attestations(
                {
                    PACKAGE: [bad_identity_item],
                    "SHA256SUMS": [bad_identity_item],
                },
                repository=REPOSITORY,
                tag=TAG,
                tag_commit=COMMIT,
                asset_digests={
                    PACKAGE: PACKAGE_DIGEST,
                    "SHA256SUMS": CHECKSUMS_DIGEST,
                },
            )

    def test_release_attestation_binds_tag_object_and_exact_assets(self) -> None:
        payload = {
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "subjectAlternativeName": "https://dotcom.releases.github.com"
                    }
                },
                "statement": {
                    "predicateType": verify_release_assets.RELEASE_ATTESTATION_V02,
                    "predicate": {"repository": REPOSITORY, "tag": TAG},
                    "subject": [
                        {
                            "uri": f"pkg:github/{REPOSITORY}@{TAG}",
                            "digest": {"sha1": TAG_OBJECT},
                        },
                        {"name": PACKAGE, "digest": {"sha256": PACKAGE_DIGEST}},
                        {
                            "name": "SHA256SUMS",
                            "digest": {"sha256": CHECKSUMS_DIGEST},
                        },
                    ],
                },
                "verifiedTimestamps": [{"type": "timestamp-authority"}],
            }
        }

        verify_release_assets.verify_release_attestation(
            payload,
            repository=REPOSITORY,
            tag=TAG,
            tag_object=TAG_OBJECT,
            asset_digests={
                PACKAGE: PACKAGE_DIGEST,
                "SHA256SUMS": CHECKSUMS_DIGEST,
            },
        )

        payload["verificationResult"]["statement"]["subject"][0]["digest"][
            "sha1"
        ] = COMMIT
        with self.assertRaisesRegex(
            verify_release_assets.VerificationError,
            "tag subject drift",
        ):
            verify_release_assets.verify_release_attestation(
                payload,
                repository=REPOSITORY,
                tag=TAG,
                tag_object=TAG_OBJECT,
                asset_digests={
                    PACKAGE: PACKAGE_DIGEST,
                    "SHA256SUMS": CHECKSUMS_DIGEST,
                },
            )


class ReleaseMetadataTests(unittest.TestCase):
    def test_release_must_be_published_immutable_and_exact(self) -> None:
        metadata = {
            "isDraft": False,
            "isImmutable": True,
            "tagName": TAG,
            "targetCommitish": COMMIT,
            "assets": [
                {
                    "name": PACKAGE,
                    "digest": f"sha256:{PACKAGE_DIGEST}",
                    "size": 100,
                    "state": "uploaded",
                },
                {
                    "name": "SHA256SUMS",
                    "digest": f"sha256:{CHECKSUMS_DIGEST}",
                    "size": 90,
                    "state": "uploaded",
                },
            ],
        }
        verify_release_assets.verify_release_metadata(
            metadata,
            tag=TAG,
            tag_commit=COMMIT,
            asset_digests={
                PACKAGE: PACKAGE_DIGEST,
                "SHA256SUMS": CHECKSUMS_DIGEST,
            },
        )

        metadata["isImmutable"] = False
        with self.assertRaisesRegex(
            verify_release_assets.VerificationError,
            "not immutable",
        ):
            verify_release_assets.verify_release_metadata(
                metadata,
                tag=TAG,
                tag_commit=COMMIT,
                asset_digests={
                    PACKAGE: PACKAGE_DIGEST,
                    "SHA256SUMS": CHECKSUMS_DIGEST,
                },
            )


class ReleaseVerificationDocumentationTests(unittest.TestCase):
    def test_canonical_command_and_two_stage_helper_migration_are_explicit(self) -> None:
        text = VERIFICATION_DOC.read_text(encoding="utf-8")
        for phrase in (
            "python3 scripts/verify_release_assets.py vX.Y.Z",
            "`SHA256SUMS` remains useful for ordinary corruption detection",
            "`git tag -v` is not the canonical authenticity check",
            "Infrastructure PR",
            "Source/helper refresh PR",
            "four v4 upload pins",
            "three v4\n   download pins",
            "470b2251cae3086d774f23afce30a1e9986ed578",
            "1a1181ee919c31a1912b3ea01b5ce0c6054e8e53",
            "plugin version `0.5.2`",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
