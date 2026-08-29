from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_commit_identity  # noqa: E402


class CommitIdentityPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self._temporary.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def commit(self, name: str, email: str, content: str) -> str:
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", name], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", email], check=True)
        (self.repo / "value.txt").write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "value.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "fixture commit"],
            check=True,
        )
        return subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()

    def audit_latest(self) -> tuple[audit_commit_identity.IdentityIssue, ...]:
        return audit_commit_identity.audit_range(
            self.repo,
            base=audit_commit_identity.ZERO_SHA,
            head="HEAD",
            protected_names=frozenset({"Oscar-V4"}),
        )

    def test_protected_public_alias_with_github_noreply_is_allowed(self) -> None:
        self.commit("Oscar-V4", "fixture@users.noreply.github.com", "safe")
        self.assertEqual(self.audit_latest(), ())

    def test_external_contributor_public_email_is_not_overridden(self) -> None:
        self.commit("External Contributor", "contributor@example.com", "safe")
        self.assertEqual(self.audit_latest(), ())

    def test_local_machine_email_is_reported_without_echoing_it(self) -> None:
        private_email = "fixture@workstation.local"
        commit = self.commit("External Contributor", private_email, "unsafe")
        issues = self.audit_latest()

        self.assertEqual(
            issues,
            (
                audit_commit_identity.IdentityIssue(commit, "author", "local-machine-email"),
                audit_commit_identity.IdentityIssue(commit, "committer", "local-machine-email"),
            ),
        )
        self.assertNotIn(private_email, "\n".join(issue.diagnostic() for issue in issues))

    def test_protected_alias_personal_email_is_reported_without_echoing_it(self) -> None:
        private_email = "fixture@example.com"
        commit = self.commit("Oscar-V4", private_email, "unsafe")
        issues = self.audit_latest()

        self.assertEqual(
            issues,
            (
                audit_commit_identity.IdentityIssue(
                    commit, "author", "protected-name-with-non-noreply-email"
                ),
                audit_commit_identity.IdentityIssue(
                    commit, "committer", "protected-name-with-non-noreply-email"
                ),
            ),
        )
        self.assertNotIn(private_email, "\n".join(issue.diagnostic() for issue in issues))


if __name__ == "__main__":
    unittest.main()
