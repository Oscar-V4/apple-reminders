from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_WORKFLOW = ROOT / ".github" / "workflows" / "prepare-signed-helper-source.yml"
LEGACY_WORKFLOW = ROOT / ".github" / "workflows" / "prepare-signed-helper.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def job_section(workflow: str, name: str, next_name: str | None) -> str:
    start = workflow.index(f"  {name}:")
    if next_name is None:
        return workflow[start:]
    end = workflow.index(f"  {next_name}:", start + 1)
    return workflow[start:end]


class SourceSigningWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = read(SOURCE_WORKFLOW)
        cls.legacy = read(LEGACY_WORKFLOW)
        cls.release = read(RELEASE_WORKFLOW)
        cls.build = job_section(
            cls.workflow,
            "test-and-build-unsigned-source",
            "sign-notarize-and-staple",
        )
        cls.sign = job_section(
            cls.workflow,
            "sign-notarize-and-staple",
            "verify-and-assemble-candidate",
        )
        cls.verify = job_section(
            cls.workflow,
            "verify-and-assemble-candidate",
            "attest-and-publish-candidate",
        )
        cls.attest = job_section(
            cls.workflow,
            "attest-and-publish-candidate",
            None,
        )

    def test_legacy_main_only_workflow_remains_the_unchanged_compatibility_path(
        self,
    ) -> None:
        self.assertIn("run-name: Prepare signed EventKit helper for ${{ github.sha }}", self.legacy)
        self.assertIn("github.ref_name == github.event.repository.default_branch", self.legacy)
        trigger = self.legacy.split("permissions:", 1)[0]
        self.assertNotIn("source_ref:", trigger)
        self.assertNotIn("source_commit:", trigger)
        self.assertNotIn("github.triggering_actor", self.legacy)

    def test_source_ref_and_commit_are_data_under_a_main_owned_workflow(self) -> None:
        for declaration in (
            "source_ref:",
            "source_commit:",
            "SOURCE_REF: ${{ inputs.source_ref }}",
            "SOURCE_COMMIT: ${{ inputs.source_commit }}",
            "WORKFLOW_COMMIT: ${{ github.sha }}",
            '[[ "$SOURCE_REF" == refs/heads/* ]]',
            'git check-ref-format "$SOURCE_REF"',
            '[[ "$(git rev-parse HEAD)" == "$SOURCE_COMMIT" ]]',
            '"+$SOURCE_REF:refs/remotes/origin/signed-helper-source"',
            '[[ "$(git rev-parse refs/remotes/origin/signed-helper-source)" == "$SOURCE_COMMIT" ]]',
            'git merge-base --is-ancestor "$WORKFLOW_COMMIT" "$SOURCE_COMMIT"',
            'git diff --exit-code "$WORKFLOW_COMMIT" "$SOURCE_COMMIT" --',
        ):
            self.assertIn(declaration, self.build)
        self.assertIn("ref: ${{ inputs.source_commit }}", self.build)
        self.assertNotIn("ref: ${{ inputs.source_ref }}", self.workflow)
        self.assertEqual(
            self.workflow.count('--workflow-commit "$WORKFLOW_COMMIT"'),
            2,
        )
        self.assertEqual(
            self.workflow.count('^[0-9a-f]{40}([0-9a-f]{24})?$'),
            8,
        )

    def test_every_job_is_main_only_and_binds_initial_and_rerun_actors(self) -> None:
        sections = (self.build, self.sign, self.verify, self.attest)
        for section in sections:
            with self.subTest(job=section.splitlines()[0].strip()):
                self.assertIn("github.actor == 'Oscar-V4'", section)
                self.assertIn("github.triggering_actor == 'Oscar-V4'", section)
                self.assertIn("github.ref_type == 'branch'", section)
                self.assertIn(
                    "github.ref_name == github.event.repository.default_branch",
                    section,
                )
        self.assertEqual(self.workflow.count('[[ "$GITHUB_ACTOR" == "Oscar-V4" ]]'), 4)
        self.assertEqual(
            self.workflow.count('[[ "$GITHUB_TRIGGERING_ACTOR" == "Oscar-V4" ]]'),
            4,
        )

    def test_protected_signing_job_has_secrets_but_no_target_checkout_or_code(self) -> None:
        self.assertIn("environment:\n      name: release-signing", self.sign)
        self.assertIn("secrets.APPLE_DEVELOPER_ID_P12_B64", self.sign)
        self.assertNotIn("actions/checkout@", self.sign)
        self.assertNotIn("actions/setup-python@", self.sign)
        self.assertNotRegex(
            self.sign,
            r"(?m)^\s+(?:python3|bash|sh)\s+(?:\./)?scripts/",
        )
        self.assertNotIn("--run-protocol-probes", self.sign)
        for section in (self.build, self.verify, self.attest):
            self.assertNotIn("secrets.", section)

    def test_target_code_runs_only_without_oidc_or_attestation_permissions(self) -> None:
        for section in (self.build, self.verify):
            self.assertIn("contents: read", section)
            self.assertNotIn("id-token: write", section)
            self.assertNotIn("attestations: write", section)
            self.assertNotIn("artifact-metadata: write", section)
        self.assertIn("python3 scripts/", self.build)
        self.assertIn("python3 scripts/", self.verify)
        self.assertIn("python3 -m unittest discover", self.verify)

    def test_stale_bundle_is_replaced_before_complete_post_sign_checks(self) -> None:
        replace = self.verify.index('rm -rf "$PLUGIN_ROOT/native"')
        audit = self.verify.index("python3 scripts/audit_source_package.py", replace)
        full_tests = self.verify.index("python3 -m unittest discover", audit)
        revalidate = self.verify.index(
            "Revalidate immutable candidate after target-code checks",
            full_tests,
        )
        upload = self.verify.index(
            "Upload immutable verified candidate for attestation",
            revalidate,
        )
        self.assertLess(replace, audit)
        self.assertLess(audit, full_tests)
        self.assertLess(full_tests, revalidate)
        self.assertLess(revalidate, upload)
        self.assertIn("EXPECTED_SIGNED_ZIP_SHA256", self.verify[revalidate:upload])
        self.assertIn("verified candidate inventory drift", self.verify[revalidate:upload])

    def test_attestation_job_has_no_checkout_or_target_code_and_rehashes_first(self) -> None:
        self.assertIn("id-token: write", self.attest)
        self.assertIn("attestations: write", self.attest)
        self.assertIn("artifact-metadata: write", self.attest)
        self.assertNotIn("actions/checkout@", self.attest)
        self.assertNotIn("actions/setup-python@", self.attest)
        self.assertNotIn("python3 scripts/", self.attest)
        self.assertNotIn("unittest", self.attest)
        validate = self.attest.index(
            "Validate attestation subjects at the final privileged boundary"
        )
        attest = self.attest.index("Attest the exact verified candidate", validate)
        between = self.attest[validate:attest]
        for evidence in (
            "attestation subject inventory drift",
            "attestation ZIP digest drift",
            "attestation manifest digest drift",
            '[[ "$EXPECTED_VERIFIED_ZIP_SHA256" == "$EXPECTED_PROTECTED_ZIP_SHA256" ]]',
            "sha256sum --check --strict SHA256SUMS",
        ):
            self.assertIn(evidence, between)
        self.assertEqual(self.workflow.count("uses: actions/attest@"), 1)

    def test_artifacts_are_run_scoped_and_all_actions_are_sha_pinned(self) -> None:
        for fragment in (
            "${{ inputs.source_commit }}-${{ github.run_id }}-${{ github.run_attempt }}",
            "${{ needs.test-and-build-unsigned-source.outputs.source_commit }}-${{ github.run_id }}-${{ github.run_attempt }}",
        ):
            self.assertIn(fragment, self.workflow)
        action_refs = re.findall(
            r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)",
            self.workflow,
        )
        self.assertEqual(len(action_refs), 12)
        for action_ref in action_refs:
            with self.subTest(action_ref=action_ref):
                self.assertRegex(action_ref, r"^[^@]+@[0-9a-f]{40}$")

    def test_new_signing_workflow_is_codeowned(self) -> None:
        codeowners = read(ROOT / ".github" / "CODEOWNERS")
        self.assertIn(
            "/.github/workflows/prepare-signed-helper-source.yml @Oscar-V4",
            codeowners,
        )

    def test_release_accepts_only_exact_legacy_or_main_owned_provenance(self) -> None:
        legacy_start = self.release.index('if [[ "$RELEASE_VERSION" == "0.5.0" ]]')
        future_start = self.release.index(
            '[[ "$MANIFEST_WORKFLOW_COMMIT" =~ ^[0-9a-f]{40}',
            legacy_start,
        )
        legacy = self.release[legacy_start:future_start]
        future = self.release[future_start:]
        for gate in (
            "prepare-signed-helper.yml",
            '--signer-digest "$MANIFEST_SOURCE_COMMIT"',
            '--source-digest "$MANIFEST_SOURCE_COMMIT"',
            '--source-ref "refs/heads/main"',
        ):
            self.assertIn(gate, legacy)
        for gate in (
            "prepare-signed-helper-source.yml",
            '--signer-digest "$MANIFEST_WORKFLOW_COMMIT"',
            '--source-digest "$MANIFEST_WORKFLOW_COMMIT"',
            '--source-ref "refs/heads/main"',
            'certificate.get("githubWorkflowSHA")',
            'certificate.get(field) != workflow_commit',
            'git merge-base --is-ancestor "$workflow_commit" "$MANIFEST_SOURCE_COMMIT"',
            'git merge-base --is-ancestor "$workflow_commit" "$TAG_COMMIT"',
            'git merge-base --is-ancestor "$workflow_commit" "refs/remotes/origin/main"',
        ):
            self.assertIn(gate, future)
        self.assertIn('payload.get("workflow_commit")', self.release)
        self.assertIn(
            "manifest workflow_commit does not match attestation",
            self.release,
        )


if __name__ == "__main__":
    unittest.main()
