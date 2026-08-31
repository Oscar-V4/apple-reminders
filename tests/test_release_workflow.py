from __future__ import annotations

import re
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def job_section(workflow: str, name: str, next_name: str | None) -> str:
    start = workflow.index(f"  {name}:")
    if next_name is None:
        return workflow[start:]
    end = workflow.index(f"  {next_name}:", start + 1)
    return workflow[start:end]


class ReleaseWorkflowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.verify = job_section(cls.text, "verify_release", "attest_release")
        cls.attest = job_section(cls.text, "attest_release", "publish_release")
        cls.publish = job_section(
            cls.text,
            "publish_release",
            "verify_published_release",
        )
        cls.post_publish = job_section(
            cls.text,
            "verify_published_release",
            None,
        )

    def test_only_strict_semver_tags_reach_a_main_ancestor_release(self) -> None:
        self.assertIn('tags:\n      - "v*"', self.text)
        strict_semver_gate = (
            '[[ "$release_tag" =~ '
            '^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$ ]]'
        )
        self.assertIn(strict_semver_gate, self.text)
        self.assertIn(
            '[[ "$RELEASE_TAG" =~ '
            '^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$ ]]',
            self.text,
        )
        for gate in (
            '[[ "$GITHUB_REF_TYPE" == "tag" ]]',
            '[[ "$remote_tag_object" == "$tag_object" ]]',
            '[[ "$(git rev-parse HEAD)" == "$tag_commit" ]]',
            'git merge-base --is-ancestor "$tag_commit" "refs/remotes/origin/main"',
            'plugin.get("version") != expected',
            'target.id == "SERVER_VERSION"',
        ):
            self.assertIn(gate, self.text)

    def test_manifest_provenance_is_bound_to_source_commit_and_workflow(self) -> None:
        for gate in (
            'git merge-base --is-ancestor "$source_commit" "$TAG_COMMIT"',
            'git merge-base --is-ancestor "$source_commit" "refs/remotes/origin/main"',
            'gh attestation verify "$manifest_path"',
            '--repo "Oscar-V4/apple-reminders"',
            '--signer-workflow "Oscar-V4/apple-reminders/.github/workflows/prepare-signed-helper.yml"',
            '--signer-digest "$MANIFEST_SOURCE_COMMIT"',
            '--source-digest "$MANIFEST_SOURCE_COMMIT"',
            '--source-ref "refs/heads/main"',
            '--predicate-type "https://slsa.dev/provenance/v1"',
            '--deny-self-hosted-runners',
        ):
            self.assertIn(gate, self.text)

    def test_expanded_helper_and_deterministic_package_are_fail_closed(self) -> None:
        for gate in (
            'native helper symlink rejected',
            'native helper special file rejected',
            'native helper directory inventory drift',
            'native helper file inventory drift',
            'manifest/app byte drift',
            '--require-developer-id',
            '--require-notarized',
            '--run-protocol-probes',
            'python3 scripts/audit_source_package.py "$PLUGIN_ROOT"',
            '--strict-worktree',
            'cmp "$build_a/$package_name" "$build_b/$package_name"',
            '--archive "$build_a/$package_name"',
        ):
            self.assertIn(gate, self.text)

    def test_every_action_is_immutably_pinned_and_no_release_secret_is_used(self) -> None:
        uses = re.findall(r"^\s*-?\s*uses:\s*([^\s]+)\s*(?:#.*)?$", self.text, re.MULTILINE)
        self.assertGreaterEqual(len(uses), 7)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@\s]+@[0-9a-f]{40}$")
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("release-signing", self.text)

    def test_embedded_python_is_syntactically_valid(self) -> None:
        lines = self.text.splitlines()
        compiled = 0
        index = 0
        while index < len(lines):
            if re.search(r"<<'PY'\s*$", lines[index]):
                start = index + 1
                index = start
                while index < len(lines) and lines[index].strip() != "PY":
                    index += 1
                self.assertLess(index, len(lines), "unterminated Python heredoc")
                source = textwrap.dedent("\n".join(lines[start:index])) + "\n"
                compile(source, f"{WORKFLOW}:{start + 1}", "exec")
                compiled += 1
            index += 1
        self.assertEqual(compiled, 9)

    def test_exact_release_subjects_are_attested_without_target_code(self) -> None:
        self.assertIn("needs: verify_release", self.attest)
        for permission in (
            "contents: read",
            "id-token: write",
            "attestations: write",
            "artifact-metadata: write",
        ):
            self.assertIn(permission, self.attest)
        for forbidden in (
            "actions/checkout@",
            "actions/setup-python@",
            "python3 scripts/",
            "unittest",
            "contents: write",
        ):
            self.assertNotIn(forbidden, self.attest)

        validate = self.attest.index(
            "Validate release subjects at the final privileged boundary"
        )
        attest = self.attest.index("Attest the exact release subjects", validate)
        boundary = self.attest[validate:attest]
        for evidence in (
            '[[ "$GITHUB_WORKFLOW_REF" == "$EXPECTED_WORKFLOW_REF" ]]',
            '[[ "$GITHUB_WORKFLOW_SHA" == "$EXPECTED_TAG_COMMIT" ]]',
            "release attestation subject inventory drift",
            "release package digest drift",
            "release checksum digest drift",
            "sha256sum --check --strict SHA256SUMS",
        ):
            self.assertIn(evidence, boundary)
        self.assertIn("uses: actions/attest@", self.attest)
        self.assertIn(
            "${{ needs.verify_release.outputs.package_name }}",
            self.attest,
        )
        self.assertIn("apple-reminders-release/SHA256SUMS", self.attest)
        self.assertEqual(self.text.count("uses: actions/attest@"), 1)

    def test_publication_rechecks_tag_digests_inventory_and_workflow_identity(
        self,
    ) -> None:
        self.assertIn("- attest_release", self.publish)
        self.assertIn("contents: write", self.publish)
        self.assertIn("attestations: read", self.publish)
        self.assertNotIn("id-token: write", self.publish)
        self.assertNotIn("attestations: write", self.publish)

        publish = self.publish.index('gh release create "$RELEASE_TAG"')
        boundary = self.publish[:publish]
        for evidence in (
            'gh attestation verify "$release_root/$PACKAGE_NAME"',
            'gh attestation verify "$release_root/SHA256SUMS"',
            '--signer-workflow "Oscar-V4/apple-reminders/.github/workflows/release.yml"',
            '--signer-digest "$EXPECTED_TAG_COMMIT"',
            '--source-digest "$EXPECTED_TAG_COMMIT"',
            '--source-ref "refs/tags/$RELEASE_TAG"',
            '--predicate-type "https://slsa.dev/provenance/v1"',
            "--deny-self-hosted-runners",
            "release attestation subject inventory drift",
            "release attestation subject digest drift",
            "release attestation workflow identity drift",
            '[[ "$current_tag_object" == "$EXPECTED_TAG_OBJECT" ]]',
            '[[ "$current_tag_commit" == "$EXPECTED_TAG_COMMIT" ]]',
            "sha256sum --check --strict SHA256SUMS",
            '[[ "$(gh release create --help)" == *"Immutable Releases"* ]]',
            '[[ "$(gh release view --help)" == *"isImmutable"* ]]',
        ):
            self.assertIn(evidence, boundary)

    def test_published_release_is_redownloaded_and_independently_verified(self) -> None:
        self.assertIn("needs: publish_release", self.post_publish)
        self.assertIn("contents: read", self.post_publish)
        self.assertIn("attestations: read", self.post_publish)
        for forbidden in (
            "contents: write",
            "id-token: write",
            "attestations: write",
            "gh release create",
        ):
            self.assertNotIn(forbidden, self.post_publish)
        self.assertIn("actions/checkout@", self.post_publish)
        self.assertIn("actions/setup-python@", self.post_publish)
        self.assertIn(
            'python3 scripts/verify_release_assets.py "$RELEASE_TAG"',
            self.post_publish,
        )
        self.assertIn('--repo "Oscar-V4/apple-reminders"', self.post_publish)

    def test_only_the_publication_job_can_write_contents(self) -> None:
        verify_start = self.text.index("  verify_release:")
        attest_start = self.text.index("  attest_release:")
        publish_start = self.text.index("  publish_release:")
        post_publish_start = self.text.index("  verify_published_release:")
        verify = self.text[verify_start:attest_start]
        attest = self.text[attest_start:publish_start]
        publish = self.text[publish_start:post_publish_start]
        post_publish = self.text[post_publish_start:]

        self.assertIn("permissions: {}", self.text[:verify_start])
        self.assertIn("contents: read", verify)
        self.assertIn("attestations: read", verify)
        self.assertNotIn("contents: write", verify)
        self.assertNotIn("gh release create", verify)
        self.assertNotIn("contents: write", attest)
        self.assertIn("- attest_release", publish)
        self.assertIn("needs.attest_release.result == 'success'", publish)
        self.assertIn("contents: write", publish)
        self.assertNotIn("contents: write", post_publish)
        self.assertEqual(self.text.count("contents: write"), 1)
        self.assertEqual(self.text.count('gh release create "$RELEASE_TAG"'), 1)
        self.assertIn('--verify-tag', publish)
        self.assertIn('[[ "$current_tag_object" == "$EXPECTED_TAG_OBJECT" ]]', publish)
        self.assertIn(
            'workflow_arguments=(--source-commit "$MANIFEST_SOURCE_COMMIT")',
            verify,
        )
        self.assertNotIn("workflow_arguments=()", verify)
        self.assertIn("release_flags=(--verify-tag)", publish)
        self.assertIn('if [[ "$RELEASE_VERSION" == 0.* ]]; then', publish)
        self.assertIn("release_flags+=(--prerelease)", publish)


if __name__ == "__main__":
    unittest.main()
