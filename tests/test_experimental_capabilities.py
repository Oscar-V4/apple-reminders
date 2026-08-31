from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import experimental_capabilities  # noqa: E402


ADAPTER_PATH = SCRIPTS / "reminders_adapter.py"
SPEC = importlib.util.spec_from_file_location(
    "reminders_adapter_experimental_capability_tests",
    ADAPTER_PATH,
)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


KNOWN_IDENTITY = experimental_capabilities.RuntimeIdentity(
    macos_version="26.5.2",
    macos_build="25F84",
    reminders_version="7.0",
    reminders_build="3976",
)
UNKNOWN_IDENTITY = experimental_capabilities.RuntimeIdentity(
    macos_version="26.6",
    macos_build="25G99",
    reminders_version="7.1",
    reminders_build="4000",
)
AVAILABLE_COMPILER = experimental_capabilities.DeveloperToolchainProbe(
    Path("/Selected/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"),
    "compiler_available",
    True,
)
UNAVAILABLE_COMPILER = experimental_capabilities.DeveloperToolchainProbe(
    None,
    "developer_directory_unselected",
    True,
)


class ExperimentalCapabilityModelTests(unittest.TestCase):
    def test_clang_shim_is_rejected_when_developer_directory_is_unselected(
        self,
    ) -> None:
        compiler_usable = mock.Mock(
            side_effect=AssertionError("compiler path must not be inspected")
        )

        def runner(
            argv: list[str], timeout: float
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv, ["/usr/bin/xcode-select", "-p"])
            self.assertEqual(timeout, 5.0)
            return subprocess.CompletedProcess(argv, 2, "", "not selected")

        with mock.patch("shutil.which", return_value="/usr/bin/clang") as which:
            probe = experimental_capabilities.resolve_selected_clang(
                runner=runner,
                selection_tool_usable=lambda _path: True,
                compiler_usable=compiler_usable,
                environment={},
            )

        self.assertFalse(probe.available)
        self.assertEqual(probe.reason_code, "developer_directory_unselected")
        which.assert_not_called()
        compiler_usable.assert_not_called()

    def test_attacker_path_clang_is_never_consulted(self) -> None:
        inspected: list[Path] = []

        def runner(
            argv: list[str], timeout: float
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                0,
                "/Selected/Developer\n",
                "",
            )

        def compiler_usable(path: Path) -> bool:
            inspected.append(path)
            return False

        with mock.patch("shutil.which", return_value="/tmp/attacker/clang") as which:
            probe = experimental_capabilities.resolve_selected_clang(
                runner=runner,
                selection_tool_usable=lambda _path: True,
                compiler_usable=compiler_usable,
                environment={},
            )

        self.assertFalse(probe.available)
        which.assert_not_called()
        self.assertEqual(
            inspected,
            [
                Path(
                    "/Selected/Developer/Toolchains/"
                    "XcodeDefault.xctoolchain/usr/bin/clang"
                ),
                Path("/Selected/Developer/usr/bin/clang"),
            ],
        )
        self.assertNotIn(Path("/tmp/attacker/clang"), inspected)

    def test_selected_developer_toolchain_returns_fixed_compiler_path(self) -> None:
        expected = AVAILABLE_COMPILER.compiler_path
        self.assertIsNotNone(expected)

        def runner(
            argv: list[str], timeout: float
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                0,
                "/Selected/Developer\n",
                "",
            )

        probe = experimental_capabilities.resolve_selected_clang(
            runner=runner,
            selection_tool_usable=lambda _path: True,
            compiler_usable=lambda path: path == expected,
            environment={},
        )

        self.assertTrue(probe.available)
        self.assertEqual(probe.compiler_path, expected)

    def test_developer_environment_override_cannot_grant_compiler_access(self) -> None:
        runner = mock.Mock(
            side_effect=AssertionError("xcode-select must not run under an override")
        )

        probe = experimental_capabilities.resolve_selected_clang(
            runner=runner,
            selection_tool_usable=lambda _path: True,
            compiler_usable=lambda _path: True,
            environment={"DEVELOPER_DIR": "/tmp/attacker/Developer"},
        )

        self.assertFalse(probe.available)
        self.assertEqual(probe.reason_code, "developer_environment_override")
        runner.assert_not_called()

    def test_repository_evidence_mapping_is_runtime_immutable(self) -> None:
        with self.assertRaises(TypeError):
            experimental_capabilities.COMPATIBILITY_ALLOWLIST[
                "url_attachment_mutation"
            ] = ()

    def test_every_private_mutation_command_has_one_capability_spec(self) -> None:
        arguments = {
            "attach_image": {"backend": "reminderkit", "image": "/tmp/input.png"},
            "replace_attachment": {"url": "https://example.com"},
        }

        for command in sorted(adapter.MUTATION_COMMANDS):
            with self.subTest(command=command):
                spec = experimental_capabilities.capability_for_adapter_command(
                    command,
                    **arguments.get(command, {}),
                )
                self.assertIsNotNone(spec)
                self.assertTrue(spec.mutation)

    def test_stable_core_is_documented_compiler_free_and_build_independent(self) -> None:
        capability = experimental_capabilities.stable_core_capability(
            UNKNOWN_IDENTITY
        )

        self.assertEqual(capability["support_tier"], "stable_core")
        self.assertEqual(capability["api_boundary"], "documented_eventkit")
        self.assertEqual(capability["compiler_requirement"], "not_required")
        self.assertEqual(capability["build_compatibility"], "not_applicable")
        self.assertTrue(capability["available"])
        self.assertFalse(capability["runtime_verification_required"])

    def test_unknown_new_build_is_explicitly_unsupported(self) -> None:
        decision = experimental_capabilities.evaluate_capability(
            "url_attachment_mutation",
            UNKNOWN_IDENTITY,
            schema_fingerprint=experimental_capabilities.ATTACHMENT_SCHEMA_FINGERPRINT,
            compiler_available=False,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "unsupported_build")
        self.assertEqual(decision.support_tier, "experimental_internals")
        self.assertEqual(decision.compiler_requirement, "not_required")
        self.assertEqual(decision.runtime_state, "runtime_unverified")

    def test_missing_exact_build_metadata_is_runtime_unverified(self) -> None:
        decision = experimental_capabilities.evaluate_capability(
            "recently_deleted_recovery",
            experimental_capabilities.RuntimeIdentity(
                macos_version="26.5.2",
                macos_build=None,
                reminders_version="7.0",
                reminders_build="3976",
            ),
            schema_fingerprint=experimental_capabilities.RECOVERY_SCHEMA_FINGERPRINT,
            compiler_available=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "runtime_unverified")
        self.assertEqual(decision.compiler_requirement, "required")

    def test_schema_drift_blocks_an_allowlisted_build(self) -> None:
        decision = experimental_capabilities.evaluate_capability(
            "url_attachment_mutation",
            KNOWN_IDENTITY,
            schema_fingerprint="0" * 64,
            compiler_available=False,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "schema_fingerprint_mismatch")
        self.assertEqual(decision.build_compatibility, "allowlisted")

    def test_missing_compiler_blocks_helper_capability(self) -> None:
        decision = experimental_capabilities.evaluate_capability(
            "image_attachment_mutation",
            KNOWN_IDENTITY,
            schema_fingerprint=experimental_capabilities.ATTACHMENT_SCHEMA_FINGERPRINT,
            compiler_available=False,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "compiler_required")
        self.assertEqual(decision.compiler_requirement, "required")

    def test_compiler_free_url_capability_can_pass_the_exact_allowlist(self) -> None:
        decision = experimental_capabilities.evaluate_capability(
            "url_attachment_mutation",
            KNOWN_IDENTITY,
            schema_fingerprint=experimental_capabilities.ATTACHMENT_SCHEMA_FINGERPRINT,
            compiler_available=False,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.build_compatibility, "allowlisted")
        self.assertEqual(decision.runtime_state, "runtime_unverified")
        self.assertEqual(decision.reason_code, "runtime_verification_required")

    def test_capability_without_compatibility_evidence_stays_disabled(self) -> None:
        decision = experimental_capabilities.evaluate_capability(
            "tag_assignment_mutation",
            KNOWN_IDENTITY,
            schema_fingerprint="1" * 64,
            compiler_available=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "runtime_unverified")
        self.assertEqual(decision.build_compatibility, "no_evidence")


class AdapterExperimentalPreflightTests(unittest.TestCase):
    def _args(self, command: str, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "command": command,
            "db": None,
            "backend": "reminderkit",
            "image": None,
            "url": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_database_selection_uses_only_read_only_connections(self) -> None:
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = [0]
        with tempfile.TemporaryDirectory() as temporary:
            stores = Path(temporary)
            database = stores / "synthetic.sqlite"
            database.touch()
            with (
                mock.patch.object(adapter, "STORES", stores),
                mock.patch.object(
                    adapter,
                    "connect_read_only",
                    return_value=connection,
                ) as connect_read_only,
                mock.patch.object(
                    adapter,
                    "connect",
                    side_effect=AssertionError("write-capable connection used"),
                ),
                mock.patch.object(
                    adapter,
                    "table_names",
                    return_value=set(adapter.REQUIRED_TABLES),
                ),
                mock.patch.object(
                    adapter,
                    "image_attachment_sync_counts",
                    return_value={},
                ),
            ):
                self.assertEqual(adapter.usable_dbs(), [database])
                adapter.db_counts(database)

        self.assertEqual(connect_read_only.call_count, 2)
        self.assertEqual(connection.close.call_count, 2)

    def test_unknown_build_fails_before_database_resolution(self) -> None:
        resolve_database = mock.Mock()
        with (
            mock.patch.object(
                adapter,
                "detect_runtime_identity",
                return_value=UNKNOWN_IDENTITY,
            ),
            mock.patch.object(adapter, "resolve_database", resolve_database),
            self.assertRaises(adapter.MutationNotStartedError) as raised,
        ):
            adapter.preflight_experimental_command(
                self._args("attach_url", url="https://example.com")
            )

        resolve_database.assert_not_called()
        self.assertEqual(raised.exception.code, "unsupported_capability")
        self.assertEqual(raised.exception.details["reason_code"], "unsupported_build")
        self.assertTrue(raised.exception.details["mutation_not_started"])

    def test_unknown_helper_build_never_probes_toolchain_or_database(self) -> None:
        resolve_compiler = mock.Mock()
        resolve_database = mock.Mock()
        with (
            mock.patch.object(
                adapter,
                "detect_runtime_identity",
                return_value=UNKNOWN_IDENTITY,
            ),
            mock.patch.object(
                adapter,
                "resolve_selected_clang",
                resolve_compiler,
            ),
            mock.patch.object(adapter, "resolve_database", resolve_database),
            self.assertRaises(adapter.MutationNotStartedError) as raised,
        ):
            adapter.preflight_experimental_command(
                self._args("attach_image", image="/tmp/input.png")
            )

        resolve_compiler.assert_not_called()
        resolve_database.assert_not_called()
        self.assertEqual(raised.exception.details["reason_code"], "unsupported_build")

    def test_unknown_build_blocks_every_private_command_before_database(
        self,
    ) -> None:
        cases = {
            **{command: {} for command in adapter.MUTATION_COMMANDS},
            "list_deleted_reminders": {},
            "read_deleted_reminder": {},
        }
        cases["attach_image"] = {
            "backend": "reminderkit",
            "image": "/tmp/input.png",
        }
        cases["replace_attachment"] = {"url": "https://example.com"}

        for command, arguments in sorted(cases.items()):
            with self.subTest(command=command):
                resolve_compiler = mock.Mock()
                resolve_database = mock.Mock()
                expected_error = (
                    adapter.MutationNotStartedError
                    if command in adapter.MUTATION_COMMANDS
                    else adapter.AdapterError
                )
                with (
                    mock.patch.object(
                        adapter,
                        "detect_runtime_identity",
                        return_value=UNKNOWN_IDENTITY,
                    ),
                    mock.patch.object(
                        adapter,
                        "resolve_selected_clang",
                        resolve_compiler,
                    ),
                    mock.patch.object(
                        adapter,
                        "resolve_database",
                        resolve_database,
                    ),
                    self.assertRaises(expected_error),
                ):
                    adapter.preflight_experimental_command(
                        self._args(command, **arguments)
                    )

                resolve_compiler.assert_not_called()
                resolve_database.assert_not_called()

    def test_incomplete_runtime_identity_fails_before_database_resolution(
        self,
    ) -> None:
        resolve_database = mock.Mock()
        incomplete = experimental_capabilities.RuntimeIdentity(
            macos_version="26.5.2",
            macos_build=None,
            reminders_version="7.0",
            reminders_build="3976",
        )
        with (
            mock.patch.object(
                adapter,
                "detect_runtime_identity",
                return_value=incomplete,
            ),
            mock.patch.object(adapter, "resolve_database", resolve_database),
            self.assertRaises(adapter.MutationNotStartedError) as raised,
        ):
            adapter.preflight_experimental_command(
                self._args("attach_url", url="https://example.com")
            )

        resolve_database.assert_not_called()
        self.assertEqual(raised.exception.details["reason_code"], "runtime_unverified")

    def test_stable_core_never_initializes_the_experimental_gate(self) -> None:
        detect = mock.Mock()
        with mock.patch.object(adapter, "detect_runtime_identity", detect):
            result = adapter.preflight_experimental_command(
                self._args("read_reminder")
            )

        self.assertIsNone(result)
        detect.assert_not_called()

    def test_main_projects_unsupported_build_without_dispatching_mutation(self) -> None:
        dispatch = mock.Mock()
        parser = mock.Mock()
        parser.parse_args.return_value = self._args(
            "attach_url",
            url="https://example.com",
            func=dispatch,
            id="11111111-1111-4111-8111-111111111111",
            list_id=None,
            section_id=None,
            attachment_id=None,
        )
        with (
            mock.patch.object(adapter, "build_parser", return_value=parser),
            mock.patch.object(
                adapter,
                "detect_runtime_identity",
                return_value=UNKNOWN_IDENTITY,
            ),
            mock.patch.object(adapter, "resolve_database") as resolve_database,
            mock.patch.object(adapter, "json_out") as output,
        ):
            exit_code = adapter.main(["attach_url"])

        dispatch.assert_not_called()
        resolve_database.assert_not_called()
        self.assertEqual(exit_code, 1)
        payload = output.call_args.args[0]
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["verification"]["write_performed"])
        self.assertEqual(payload["error"]["code"], "unsupported_capability")
        self.assertEqual(payload["error"]["reason_code"], "unsupported_build")
        self.assertEqual(
            payload["error"]["capability"]["support_tier"],
            "experimental_internals",
        )

    def test_missing_compiler_fails_before_database_resolution(self) -> None:
        resolve_database = mock.Mock()
        with (
            mock.patch.object(
                adapter,
                "detect_runtime_identity",
                return_value=KNOWN_IDENTITY,
            ),
            mock.patch.object(
                adapter,
                "resolve_selected_clang",
                return_value=UNAVAILABLE_COMPILER,
            ),
            mock.patch.object(adapter, "resolve_database", resolve_database),
            self.assertRaises(adapter.MutationNotStartedError) as raised,
        ):
            adapter.preflight_experimental_command(
                self._args("attach_image", image="/tmp/input.png")
            )

        resolve_database.assert_not_called()
        self.assertEqual(raised.exception.details["reason_code"], "compiler_required")

    def test_schema_drift_fails_before_mutation_dispatch(self) -> None:
        connection = mock.Mock()
        connection.close = mock.Mock()
        with (
            mock.patch.object(
                adapter,
                "detect_runtime_identity",
                return_value=KNOWN_IDENTITY,
            ),
            mock.patch.object(
                adapter,
                "resolve_selected_clang",
                return_value=AVAILABLE_COMPILER,
            ),
            mock.patch.object(
                adapter,
                "resolve_database",
                return_value=Path("/synthetic/store.sqlite"),
            ),
            mock.patch.object(adapter, "connect_read_only", return_value=connection),
            mock.patch.object(
                adapter,
                "command_capability",
                return_value={
                    "command": "attachment_mutation_db",
                    "supported": True,
                    "missing_tables": [],
                    "missing_columns": {},
                    "schema_fingerprint": "0" * 64,
                },
            ),
            self.assertRaises(adapter.MutationNotStartedError) as raised,
        ):
            adapter.preflight_experimental_command(
                self._args("attach_image", image="/tmp/input.png")
            )

        connection.close.assert_called_once()
        self.assertEqual(
            raised.exception.details["reason_code"],
            "schema_fingerprint_mismatch",
        )

    def test_helper_builders_use_selected_compiler_not_path(self) -> None:
        selected = AVAILABLE_COMPILER.compiler_path
        self.assertIsNotNone(selected)
        failed_build = subprocess.CompletedProcess(
            [str(selected)],
            1,
            "",
            "synthetic compile failure",
        )
        for builder in (
            adapter.reminderkit_attach_helper,
            adapter.reminderkit_sections_helper,
            adapter.reminderkit_recover_helper,
        ):
            with (
                self.subTest(builder=builder.__name__),
                tempfile.TemporaryDirectory() as temporary,
            ):
                with (
                    mock.patch.object(adapter, "CACHE_DIR", Path(temporary)),
                    mock.patch.object(
                        adapter,
                        "resolve_selected_clang",
                        return_value=AVAILABLE_COMPILER,
                    ),
                    mock.patch.object(
                        adapter,
                        "run_bounded_process",
                        return_value=failed_build,
                    ) as run,
                    mock.patch(
                        "shutil.which",
                        return_value="/tmp/attacker/clang",
                    ) as which,
                    self.assertRaises(adapter.AdapterError),
                ):
                    builder()

            which.assert_not_called()
            self.assertEqual(Path(run.call_args.args[0][0]), selected)

    def test_recovery_guard_mismatch_never_dispatches_helper(self) -> None:
        reminder_id = "11111111-1111-4111-8111-111111111111"
        list_id = "22222222-2222-4222-8222-222222222222"
        connection = mock.Mock()
        invoke = mock.Mock()
        args = argparse.Namespace(
            db=None,
            id=reminder_id,
            list_id=list_id,
            if_store_identity="expected-store",
            if_version=3,
            if_deleted_at="2026-08-28T00:00:00+09:00",
            if_attachment_digest="a" * 64,
            if_native_guard_digest="b" * 64,
        )
        actual_guard = {
            "store_identity": "changed-store",
            "private_version": 3,
            "deleted_at": args.if_deleted_at,
            "attachment_digest": args.if_attachment_digest,
        }
        with (
            mock.patch.object(
                adapter,
                "resolve_database",
                return_value=Path("/synthetic/store.sqlite"),
            ),
            mock.patch.object(adapter, "connect_read_only", return_value=connection),
            mock.patch.object(
                adapter,
                "find_deleted_reminder",
                return_value={"ZACCOUNT": 1},
            ),
            mock.patch.object(adapter, "find_list", return_value={"ZACCOUNT": 1}),
            mock.patch.object(
                adapter,
                "deleted_reminder_snapshot",
                return_value=(
                    {
                        "id": reminder_id,
                        "deleted_at": args.if_deleted_at,
                        "attachment_count": 0,
                    },
                    actual_guard,
                ),
            ),
            mock.patch.object(
                adapter,
                "deleted_store_identity",
                return_value=actual_guard["store_identity"],
            ),
            mock.patch.object(adapter, "invoke_reminderkit_recovery", invoke),
            self.assertRaises(adapter.AdapterError) as raised,
        ):
            adapter.recover_deleted_reminder_once(args)

        invoke.assert_not_called()
        self.assertEqual(raised.exception.code, "concurrent_modification")
        self.assertIn("store_identity", raised.exception.details["mismatched_fields"])


if __name__ == "__main__":
    unittest.main()
