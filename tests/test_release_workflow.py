from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class ReleaseWorkflowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

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
        self.assertGreaterEqual(len(uses), 4)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@\s]+@[0-9a-f]{40}$")
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("release-signing", self.text)

    def test_only_the_final_publication_job_can_write_contents(self) -> None:
        verify_start = self.text.index("  verify_release:")
        publish_start = self.text.index("  publish_release:")
        verify = self.text[verify_start:publish_start]
        publish = self.text[publish_start:]

        self.assertIn("permissions: {}", self.text[:verify_start])
        self.assertIn("contents: read", verify)
        self.assertIn("attestations: read", verify)
        self.assertNotIn("contents: write", verify)
        self.assertNotIn("gh release create", verify)
        self.assertIn("needs: verify_release", publish)
        self.assertIn("needs.verify_release.result == 'success'", publish)
        self.assertIn("contents: write", publish)
        self.assertEqual(self.text.count("contents: write"), 1)
        self.assertEqual(self.text.count("gh release create"), 1)
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
