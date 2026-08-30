from __future__ import annotations

import json
import os
import plistlib
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_eventkit_helper_app  # noqa: E402
import verify_eventkit_helper  # noqa: E402


class EventKitHelperDistributionTests(unittest.TestCase):
    def test_versioned_plist_uses_plugin_version_and_separate_stable_identity(self) -> None:
        payload = plistlib.loads(
            build_eventkit_helper_app.versioned_info_plist(
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT
            )
        )
        legacy = plistlib.loads(
            (
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT
                / "scripts"
                / "eventkit_bridge_info.plist"
            ).read_bytes()
        )

        self.assertEqual(payload["CFBundleShortVersionString"], "0.5.0")
        self.assertEqual(payload["CFBundleVersion"], "0.5.0")
        self.assertEqual(
            payload["CFBundleIdentifier"],
            build_eventkit_helper_app.BUNDLE_IDENTIFIER,
        )
        self.assertEqual(
            payload["CFBundleExecutable"],
            build_eventkit_helper_app.EXECUTABLE_NAME,
        )
        self.assertEqual(
            legacy["CFBundleIdentifier"],
            "com.codex.apple-reminders.eventkit-bridge",
        )
        self.assertEqual(legacy["CFBundleExecutable"], "reminders-eventkit")
        self.assertNotEqual(
            legacy["CFBundleIdentifier"],
            payload["CFBundleIdentifier"],
        )

    def test_manifest_inputs_exclude_the_legacy_runtime_plist(self) -> None:
        source_paths = {
            build_eventkit_helper_app.MANIFEST_RELATIVE_PATH,
            build_eventkit_helper_app.SOURCE_RELATIVE_PATH,
            build_eventkit_helper_app.SCHEMA_RELATIVE_PATH,
        }
        build_paths = set(build_eventkit_helper_app.BUILD_INPUT_RELATIVE_PATHS)

        self.assertNotIn(Path("scripts/eventkit_bridge_info.plist"), source_paths)
        self.assertNotIn(Path("scripts/eventkit_bridge_info.plist"), build_paths)
        self.assertIn(
            Path("scripts/eventkit_helper_app_info.plist"),
            build_paths,
        )

    def test_force_build_rejects_output_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            victim = root / "victim"
            victim.mkdir()
            sentinel = victim / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            output = root / build_eventkit_helper_app.APP_NAME
            output.symlink_to(victim, target_is_directory=True)

            with self.assertRaises(build_eventkit_helper_app.BuildFailure):
                build_eventkit_helper_app.build_app(
                    build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                    output,
                    identity="-",
                    force=True,
                )

            self.assertTrue(output.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_failed_force_build_preserves_existing_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / build_eventkit_helper_app.APP_NAME
            output.mkdir()
            sentinel = output / "sentinel"
            sentinel.write_text("existing", encoding="utf-8")
            with (
                mock.patch.object(
                    build_eventkit_helper_app,
                    "run",
                    side_effect=build_eventkit_helper_app.BuildFailure("compile failed"),
                ),
                self.assertRaises(build_eventkit_helper_app.BuildFailure),
            ):
                build_eventkit_helper_app.build_app(
                    build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                    output,
                    identity="-",
                    force=True,
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing")

    def test_verifier_rejects_app_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.app"
            target.mkdir()
            app = root / build_eventkit_helper_app.APP_NAME
            app.symlink_to(target, target_is_directory=True)

            with self.assertRaises(build_eventkit_helper_app.BuildFailure):
                verify_eventkit_helper.verify_app(
                    build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                    app,
                )

    @unittest.skipUnless(sys.platform == "darwin", "macOS helper build requires Xcode")
    def test_ad_hoc_build_is_universal_executable_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Path(temp_dir) / build_eventkit_helper_app.APP_NAME
            build_eventkit_helper_app.build_app(
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                app,
                identity="-",
            )
            with mock.patch.object(
                verify_eventkit_helper,
                "_protocol_probe",
            ) as protocol_probe:
                result = verify_eventkit_helper.verify_app(
                    build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                    app,
                )
            executable_mode = stat.S_IMODE(
                (
                    app
                    / "Contents/MacOS"
                    / build_eventkit_helper_app.EXECUTABLE_NAME
                ).stat().st_mode
            )

        protocol_probe.assert_not_called()
        self.assertEqual(result["architectures"], ["arm64", "x86_64"])
        self.assertEqual(result["minimum_macos"], "14.0")
        self.assertEqual(
            result["bundle_identifier"],
            build_eventkit_helper_app.BUNDLE_IDENTIFIER,
        )
        self.assertEqual(executable_mode, 0o755)

    @unittest.skipUnless(sys.platform == "darwin", "macOS helper build requires Xcode")
    def test_protocol_execution_is_explicitly_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Path(temp_dir) / build_eventkit_helper_app.APP_NAME
            build_eventkit_helper_app.build_app(
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                app,
                identity="-",
            )
            with mock.patch.object(
                verify_eventkit_helper,
                "_protocol_probe",
            ) as protocol_probe:
                verify_eventkit_helper.verify_app(
                    build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                    app,
                    run_protocol_probes=True,
                )

        self.assertEqual(
            [call.args[1] for call in protocol_probe.call_args_list],
            ["schema", "capabilities"],
        )

    @unittest.skipUnless(sys.platform == "darwin", "macOS helper build requires Xcode")
    def test_verifier_rejects_an_extra_bundle_file_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Path(temp_dir) / build_eventkit_helper_app.APP_NAME
            build_eventkit_helper_app.build_app(
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                app,
                identity="-",
            )
            unexpected = app / "Contents" / "Resources" / "unexpected.txt"
            unexpected.parent.mkdir()
            unexpected.write_text("not reviewed", encoding="utf-8")

            with (
                mock.patch.object(verify_eventkit_helper, "_protocol_probe") as probe,
                self.assertRaisesRegex(
                    build_eventkit_helper_app.BuildFailure,
                    "file inventory drift",
                ),
            ):
                verify_eventkit_helper.verify_app(
                    build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                    app,
                    run_protocol_probes=True,
                )

        probe.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "macOS helper build requires Xcode")
    def test_manifest_verification_requires_callers_source_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = root / build_eventkit_helper_app.APP_NAME
            build_eventkit_helper_app.build_app(
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                app,
                identity="-",
            )
            actual = verify_eventkit_helper.verify_app(
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                app,
            )
            manifest = verify_eventkit_helper.build_manifest(
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                actual,
                source_commit="a" * 40,
            )
            manifest_path = root / "eventkit-helper-build.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(build_eventkit_helper_app.BuildFailure):
                verify_eventkit_helper.verify_manifest(
                    build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                    manifest_path,
                    actual,
                    expected_source_commit="b" * 40,
                )

    @unittest.skipUnless(sys.platform == "darwin", "macOS helper build requires Xcode")
    def test_builder_normalizes_bundle_modes_under_restrictive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Path(temp_dir) / build_eventkit_helper_app.APP_NAME
            previous_umask = os.umask(0o077)
            try:
                build_eventkit_helper_app.build_app(
                    build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                    app,
                    identity="-",
                )
            finally:
                os.umask(previous_umask)

            verify_eventkit_helper.verify_app(
                build_eventkit_helper_app.DEFAULT_PLUGIN_ROOT,
                app,
            )
            self.assertEqual(stat.S_IMODE(app.stat().st_mode), 0o755)
            self.assertEqual(
                stat.S_IMODE((app / "Contents/Info.plist").stat().st_mode),
                0o644,
            )


class SigningWorkflowBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (
            REPO_ROOT / ".github" / "workflows" / "prepare-signed-helper.yml"
        ).read_text(encoding="utf-8")
        cls.protected = cls.workflow.split(
            "  sign-notarize-and-staple:", 1
        )[1].split("  verify-attest-and-publish-candidate:", 1)[0]
        cls.pre_signing = cls.workflow.split("  sign-notarize-and-staple:", 1)[0]
        cls.post_signing = cls.workflow.split(
            "  verify-attest-and-publish-candidate:", 1
        )[1]

    def test_protected_job_has_no_checkout_setup_or_repo_script_execution(self) -> None:
        self.assertNotIn("actions/checkout@", self.protected)
        self.assertNotIn("actions/setup-python@", self.protected)
        self.assertNotRegex(
            self.protected,
            r"(?m)^\s+(?:python3|bash|sh)\s+(?:\./)?scripts/",
        )
        self.assertNotIn("--run-protocol-probes", self.protected)
        self.assertIn("--run-protocol-probes", self.pre_signing)
        self.assertIn("--run-protocol-probes", self.post_signing)

    def test_workflow_separates_build_signing_and_secret_free_verification(self) -> None:
        for job in (
            "test-and-build-unsigned",
            "sign-notarize-and-staple",
            "verify-attest-and-publish-candidate",
        ):
            self.assertIn(f"  {job}:", self.workflow)
        self.assertIn("environment:\n      name: release-signing", self.protected)
        self.assertIn("trap cleanup EXIT", self.protected)
        self.assertIn("cleanup\n          set -e\n          trap - EXIT", self.protected)
        self.assertGreaterEqual(self.protected.count("umask 022"), 2)
        self.assertIn(
            "umask 022\n            codesign --remove-signature",
            self.protected,
        )
        self.assertIn(
            "umask 022\n            xcrun stapler staple",
            self.protected,
        )
        self.assertIn("Confirm protected credentials are gone", self.protected)
        self.assertIn("Attest the exact verified candidate", self.post_signing)
        self.assertNotIn("vars.APPLE_TEAM_ID", self.post_signing)
        self.assertNotIn("outputs.team_id", self.workflow)
        self.assertIn("APPLE_TEAM_ID: V8347N9346", self.post_signing)
        self.assertIn(
            '[[ "$APPLE_TEAM_ID" == "V8347N9346" ]]',
            self.protected,
        )
        self.assertIn("github.repository == 'Oscar-V4/apple-reminders'", self.workflow)
        self.assertEqual(self.workflow.count("runs-on: macos-15"), 3)
        self.assertNotIn("runs-on: macos-14", self.workflow)

    def test_archives_are_checked_before_manual_extraction_in_both_boundaries(self) -> None:
        for section in (self.protected, self.post_signing):
            self.assertIn("central-directory inventory drift", section)
            self.assertIn("len(names) != len(set(names))", section)
            self.assertIn("stat.S_IFMT(mode) != stat.S_IFREG", section)
            self.assertIn("exceeds the total size budget", section)

    def test_all_actions_are_immutable_sha_pinned(self) -> None:
        action_refs = re.findall(
            r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)",
            self.workflow,
        )
        self.assertEqual(len(action_refs), 10)
        self.assertEqual(
            {action_ref.partition("@")[0] for action_ref in action_refs},
            {
                "actions/attest",
                "actions/checkout",
                "actions/download-artifact",
                "actions/setup-python",
                "actions/upload-artifact",
            },
        )
        for action_ref in action_refs:
            with self.subTest(action_ref=action_ref):
                self.assertRegex(action_ref, r"^[^@]+@[0-9a-f]{40}$")

    def test_codeowners_covers_the_signing_boundary(self) -> None:
        codeowners = (REPO_ROOT / ".github" / "CODEOWNERS").read_text(
            encoding="utf-8"
        )
        for path in (
            "/.gitattributes",
            "/.github/CODEOWNERS",
            "/.github/workflows/prepare-signed-helper.yml",
            "/.github/workflows/release.yml",
            "/plugins/apple-reminders/.codex-plugin/plugin.json",
            "/plugins/apple-reminders/native/**",
            "/plugins/apple-reminders/scripts/eventkit_bridge.py",
            "/scripts/audit_source_package.py",
            "/scripts/build_eventkit_helper_app.py",
            "/scripts/build_source_package.py",
            "/scripts/eventkit_helper_app_info.plist",
            "/scripts/setup_release_signing_credentials.sh",
            "/scripts/verify_eventkit_helper.py",
        ):
            self.assertIn(path, codeowners)

    def test_signed_and_attested_helper_bytes_disable_text_conversion(self) -> None:
        attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        for path in (
            "plugins/apple-reminders/native/AppleRemindersEventKitHelper.app/Contents/Info.plist",
            "plugins/apple-reminders/native/AppleRemindersEventKitHelper.app/Contents/MacOS/apple-reminders-eventkit-helper",
            "plugins/apple-reminders/native/AppleRemindersEventKitHelper.app/Contents/_CodeSignature/CodeResources",
            "plugins/apple-reminders/native/AppleRemindersEventKitHelper.app/Contents/CodeResources",
            "plugins/apple-reminders/native/eventkit-helper-build.json",
        ):
            self.assertIn(f"{path} -text", attributes)

    def test_setup_wizard_is_fail_closed_to_the_one_repository(self) -> None:
        wizard = (
            REPO_ROOT / "scripts" / "setup_release_signing_credentials.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('EXPECTED_REPO_SLUG="Oscar-V4/apple-reminders"', wizard)
        self.assertIn('[[ "$REPO_SLUG" == "$EXPECTED_REPO_SLUG" ]]', wizard)
        self.assertIn('[[ "$P8_PATH" == "$REPO_ROOT"', wizard)
        self.assertIn('[[ "$APPLE_TEAM_ID" == "V8347N9346" ]]', wizard)
        self.assertIn("Git 저장소 안에 둘 수 없습니다", wizard)
        self.assertIn("새 키를 만들지 말고", wizard)
        self.assertIn("credentials must be re-entered", wizard)
        self.assertIn("기존 설정은 변경하지 않았습니다", wizard)
        self.assertIn("deployment-branch-policies", wizard)
        self.assertIn('APPLE_NOTARY_KEY_ID" =~ ^[A-Z0-9]{10}$', wizard)

        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("*.p8", gitignore)
        self.assertIn("*.p12", gitignore)


if __name__ == "__main__":
    unittest.main()
