from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_public_claims.py"
CLAIM_FILES = (
    Path("README.md"),
    Path("CHANGELOG.md"),
    Path("PRIVACY.md"),
    Path("SECURITY.md"),
    Path("SUPPORT.md"),
    Path("plugins/apple-reminders/README.md"),
    Path("plugins/apple-reminders/CHANGELOG.md"),
    Path("plugins/apple-reminders/PRIVACY.md"),
    Path("plugins/apple-reminders/SECURITY.md"),
    Path("plugins/apple-reminders/SUPPORT.md"),
    Path("plugins/apple-reminders/.codex-plugin/plugin.json"),
    Path("plugins/apple-reminders/native/eventkit-helper-build.json"),
    Path("docs/launch/public-beta-launch-kit.md"),
    Path("docs/launch/v0.4.0-ready-copy.md"),
    Path("docs/launch/external-tester-workflow.md"),
    Path("docs/launch/external-tester-receipt.schema.json"),
    Path("docs/launch/examples/external-tester-receipt.example.json"),
    Path("docs/launch/assets/README.md"),
    Path("docs/launch/assets/apple-reminders-social-preview.png"),
    Path("docs/release-verification.md"),
    Path("docs/installation.md"),
    Path("docs/decisions/0020-fail-closed-experimental-runtime-gate.md"),
    Path("scripts/verify_release_assets.py"),
    Path(".github/workflows/release.yml"),
)


class PublicBetaClaimTests(unittest.TestCase):
    def copy_claim_tree(self, destination: Path) -> None:
        for relative in CLAIM_FILES:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / relative, target)

    def run_checker(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(root)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def replace(self, root: Path, relative: Path, old: str, new: str) -> None:
        path = root / relative
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def test_current_public_surfaces_pass_the_claim_checker(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CHECKER)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "Public beta claim check passed\n")
        self.assertEqual(completed.stderr, "")

    def test_blanket_xcode_requirement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.copy_claim_tree(root)
            launch = root / "docs/launch/public-beta-launch-kit.md"
            launch.write_text(
                launch.read_text(encoding="utf-8")
                + "\nRequires Python 3.11+, Xcode Command Line Tools, and Reminders permission.\n",
                encoding="utf-8",
            )

            completed = self.run_checker(root)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("blanket Xcode requirement", completed.stderr)

    def test_each_public_surface_has_a_semantic_drift_guard(self) -> None:
        cases = (
            (
                "readme",
                (Path("README.md"), Path("plugins/apple-reminders/README.md")),
                "You do not\nneed Xcode",
                "You need Xcode",
            ),
            (
                "installation",
                (Path("docs/installation.md"),),
                "does not bypass admission",
                "bypasses admission",
            ),
            (
                "release notes",
                (Path("CHANGELOG.md"), Path("plugins/apple-reminders/CHANGELOG.md")),
                "canonical semantic",
                "legacy semantic",
            ),
            (
                "privacy",
                (Path("PRIVACY.md"), Path("plugins/apple-reminders/PRIVACY.md")),
                "undocumented and version-sensitive",
                "private",
            ),
            (
                "security",
                (Path("SECURITY.md"), Path("plugins/apple-reminders/SECURITY.md")),
                "Routine fields use EventKit",
                "Routine fields use private storage",
            ),
            (
                "support",
                (Path("SUPPORT.md"), Path("plugins/apple-reminders/SUPPORT.md")),
                "minimal, redacted reproduction",
                "full reproduction",
            ),
            (
                "plugin card",
                (Path("plugins/apple-reminders/.codex-plugin/plugin.json"),),
                "Core runs locally on macOS 14+ without Xcode",
                "Core runs locally",
            ),
            (
                "launch copy",
                (Path("docs/launch/public-beta-launch-kit.md"),),
                "release candidate",
                "public prerelease",
            ),
        )

        for name, paths, old, new in cases:
            with self.subTest(surface=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self.copy_claim_tree(root)
                for relative in paths:
                    self.replace(root, relative, old, new)

                completed = self.run_checker(root)

                self.assertEqual(completed.returncode, 1, completed.stdout)

    def test_readme_allows_copy_edits_without_reintroducing_advanced_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.copy_claim_tree(root)
            for relative in (Path("README.md"), Path("plugins/apple-reminders/README.md")):
                self.replace(root, relative, "Python 3.11 or newer", "Python 3.11+")
                self.replace(root, relative, "You do not\nneed Xcode", "You do not\nrequire Xcode")
                self.replace(root, relative, "latest published release", "current published release")
                text = (root / relative).read_text(encoding="utf-8")
                self.assertNotIn("xcode-select", text)
                self.assertNotIn("/usr/bin/clang", text)
                self.assertNotIn("schema fingerprint", text)

            completed = self.run_checker(root)

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_readme_requires_a_positive_python_dependency_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.copy_claim_tree(root)
            for relative in (Path("README.md"), Path("plugins/apple-reminders/README.md")):
                self.replace(
                    root,
                    relative,
                    "The plugin still needs **Python 3.11 or newer**.",
                    "The plugin includes everything it needs.",
                )

            completed = self.run_checker(root)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("explicit Python runtime requirement", completed.stderr)

    def test_published_and_development_contracts_cannot_be_conflated(self) -> None:
        cases = (
            (
                (Path("README.md"), Path("plugins/apple-reminders/README.md")),
                "Unreleased development branch",
                "Already available in the installed release",
                "version boundary",
            ),
            (
                (Path("README.md"), Path("plugins/apple-reminders/README.md")),
                "15 tools, including experimental tools",
                "9 Core and diagnostic tools by default",
                "tool inventory drift",
            ),
            (
                (Path("README.md"), Path("plugins/apple-reminders/README.md")),
                "Combines EventKit storage with native URL attachment work; it can partly succeed",
                "Stores EventKit URL metadata only",
                "URL behavior drift",
            ),
            (
                (Path("docs/installation.md"),),
                "Static 15-tool interface",
                "Static 9-tool interface",
                "tool inventory drift",
            ),
            (
                (Path("docs/installation.md"),),
                "EventKit plus native URL attachment composition",
                "EventKit metadata only",
                "URL behavior drift",
            ),
        )
        for paths, old, new, expected_error in cases:
            with self.subTest(surface=paths[0], claim=old), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self.copy_claim_tree(root)
                for relative in paths:
                    self.replace(root, relative, old, new)

                completed = self.run_checker(root)

                self.assertEqual(completed.returncode, 1)
                self.assertIn(expected_error, completed.stderr)

    def test_installation_guide_keeps_opt_in_and_clean_mac_limits(self) -> None:
        for old, new in (
            (
                "Disabled tools are rejected unless the runtime started with `--experimental`",
                "Disabled tools can be called directly by name",
            ),
            (
                "still need acceptance testing on fresh nondeveloper Macs",
                "have passed acceptance testing on fresh nondeveloper Macs",
            ),
        ):
            with self.subTest(claim=old), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self.copy_claim_tree(root)
                self.replace(root, Path("docs/installation.md"), old, new)

                completed = self.run_checker(root)

                self.assertEqual(completed.returncode, 1)
                self.assertIn("docs/installation.md: missing", completed.stderr)

    def test_installation_guide_rejects_unsafe_readiness_and_all_mac_claims(self) -> None:
        cases = (
            ("This plugin is production-ready.", "forbidden readiness or affiliation claim"),
            ("This plugin works on every Mac.", "unsupported universal Mac compatibility claim"),
            ("This plugin runs on all macOS versions.", "unsupported universal Mac compatibility claim"),
        )
        for claim, expected_error in cases:
            with self.subTest(claim=claim), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self.copy_claim_tree(root)
                guide = root / "docs/installation.md"
                guide.write_text(guide.read_text(encoding="utf-8") + f"\n{claim}\n", encoding="utf-8")

                completed = self.run_checker(root)

                self.assertEqual(completed.returncode, 1)
                self.assertIn(expected_error, completed.stderr)

    def test_launch_copy_rejects_placeholders_and_banned_claims(self) -> None:
        cases = ("<TAG>", "official", "approved", "certified", "production-ready")
        for value in cases:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self.copy_claim_tree(root)
                launch = root / "docs/launch/public-beta-launch-kit.md"
                launch.write_text(
                    launch.read_text(encoding="utf-8") + f"\n{value}\n",
                    encoding="utf-8",
                )

                completed = self.run_checker(root)

                self.assertEqual(completed.returncode, 1)

    def test_final_evidence_sources_have_drift_guards(self) -> None:
        cases = (
            (
                "helper version",
                Path("plugins/apple-reminders/native/eventkit-helper-build.json"),
                '"plugin_version": "0.5.2"',
                '"plugin_version": "0.5.9"',
            ),
            (
                "canonical release remote",
                Path("scripts/verify_release_assets.py"),
                'REPOSITORY = "Oscar-V4/apple-reminders"',
                'REPOSITORY = "fork/apple-reminders"',
            ),
            (
                "release verification",
                Path("docs/release-verification.md"),
                "mutable `origin` is never trusted for source identity",
                "mutable `origin` supplies source identity",
            ),
            (
                "experimental compiler selection",
                Path("docs/decisions/0020-fail-closed-experimental-runtime-gate.md"),
                "never trusts `PATH` or the `/usr/bin/clang`",
                "trusts `PATH` clang",
            ),
        )

        for name, relative, old, new in cases:
            with self.subTest(evidence=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self.copy_claim_tree(root)
                self.replace(root, relative, old, new)

                completed = self.run_checker(root)

                self.assertEqual(completed.returncode, 1, completed.stdout)

    def test_social_preview_bytes_are_bound_to_the_documented_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.copy_claim_tree(root)
            preview = root / "docs/launch/assets/apple-reminders-social-preview.png"
            preview.write_bytes(preview.read_bytes() + b"changed")

            completed = self.run_checker(root)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("social preview digest drift", completed.stderr)


if __name__ == "__main__":
    unittest.main()
