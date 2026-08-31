from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
DOCTOR_PATH = PLUGIN_ROOT / "scripts" / "reminders_doctor.py"
SPEC = importlib.util.spec_from_file_location("reminders_doctor", DOCTOR_PATH)
assert SPEC and SPEC.loader
reminders_doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reminders_doctor)


DARWIN_INFO = {
    "system": "Darwin",
    "macos_version": "26.5",
    "kernel_release": "25.5.0",
    "machine": "arm64",
    "python_version": "3.14.6",
}


def fake_runner(
    argv: list[str], *, timeout: float = 20.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, "", "")


def fake_which(command: str) -> str | None:
    return (
        f"/usr/bin/{command}"
        if command in {"clang", "xcode-select"}
        else None
    )


class DoctorFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.scripts = root / "scripts"
        self.home.mkdir()
        self.scripts.mkdir()
        self.paths = reminders_doctor.default_paths(
            home=self.home, script_dir=self.scripts
        )
        self.app = root / "System/Applications/Reminders.app"
        info_dir = self.app / "Contents"
        info_dir.mkdir(parents=True)
        with (info_dir / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": "com.apple.reminders",
                    "CFBundleShortVersionString": "7.0",
                    "CFBundleVersion": "3976",
                },
                handle,
            )
        self.paths["reminders_app_candidates"] = [self.app]

        private = {}
        for name in ("ReminderKit", "ReminderKitInternal"):
            binary = root / f"{name}.framework/{name}"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"static-test-framework")
            private[name] = binary
        self.paths["private_frameworks"] = private

        public = {}
        for name in ("Foundation", "AppKit", "ImageIO"):
            framework = root / f"{name}.framework"
            framework.mkdir()
            public[name] = framework
        self.paths["public_frameworks"] = public

        self.paths["helper_source"].write_text(
            "#import <Foundation/Foundation.h>\nint main(void) { return 0; }\n",
            encoding="utf-8",
        )
        self.paths["adapter_source"].write_text(
            "\n".join(
                (
                    "SENSITIVE_LOG_KEY = 'test'",
                    "def redacted_log_value(value): pass",
                    "def redact_log_payload(value): pass",
                    "JOURNAL_MAX_BYTES = 1000000",
                    "JOURNAL_RETENTION_DAYS = 30",
                )
            ),
            encoding="utf-8",
        )

        self.paths["stores"].mkdir(parents=True)
        self.paths["files"].mkdir()
        (self.paths["files"] / "Account-DO-NOT-REPORT").mkdir()
        self.database = self.paths["stores"] / "secret-account-id.sqlite"
        self.create_full_schema(self.database)

    @staticmethod
    def all_schema() -> dict[str, set[str]]:
        schema: dict[str, set[str]] = {
            table: set() for table in reminders_doctor.REQUIRED_TABLES
        }
        schema["ZREMCDACCOUNT"] = {"Z_PK", "ZNAME"}
        for requirements in reminders_doctor.COMMAND_SCHEMA_REQUIREMENTS.values():
            for table, columns in requirements.items():
                schema.setdefault(table, set()).update(columns)
        return schema

    @classmethod
    def create_full_schema(cls, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            for table, columns in sorted(cls.all_schema().items()):
                definitions = []
                for column in sorted(columns or {"Z_PK"}):
                    kind = "INTEGER" if column == "Z_PK" else "TEXT"
                    definitions.append(f'"{column}" {kind}')
                connection.execute(
                    f'create table "{table}" ({", ".join(definitions)})'
                )
            connection.execute(
                "insert into ZREMCDACCOUNT (Z_PK, ZNAME) values (1, ?)",
                ("private@example.com",),
            )
            connection.execute(
                "insert into ZREMCDREMINDER (Z_PK, ZTITLE) values (1, ?)",
                ("SECRET REMINDER TITLE",),
            )
            connection.commit()
        finally:
            connection.close()

    def make_artifacts_private(self) -> None:
        self.paths["app_support"].mkdir(mode=0o700, parents=True)
        os.chmod(self.paths["app_support"], 0o700)
        self.paths["journal"].write_text(
            '{"payload":{"title":"NEVER REPORT THIS"}}\n', encoding="utf-8"
        )
        os.chmod(self.paths["journal"], 0o600)
        self.paths["backups"].mkdir(mode=0o700)
        os.chmod(self.paths["backups"], 0o700)
        (self.paths["backups"] / "sensitive-backup.tgz").write_bytes(b"archive")
        self.paths["cache_dir"].mkdir(mode=0o700, parents=True)
        os.chmod(self.paths["cache_dir"], 0o700)
        self.paths["cache_file"].write_text(
            '{"title":"CACHE SECRET"}\n', encoding="utf-8"
        )
        os.chmod(self.paths["cache_file"], 0o600)
        self.paths["helper_binary"].write_bytes(b"executable")
        os.chmod(self.paths["helper_binary"], 0o700)


class PlatformAndApplicationTests(unittest.TestCase):
    def test_platform_reports_version_without_running_commands(self) -> None:
        result = reminders_doctor.inspect_platform(DARWIN_INFO)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["details"]["macos_version"], "26.5")

    def test_non_macos_is_a_stable_blocker(self) -> None:
        result = reminders_doctor.inspect_platform(
            {**DARWIN_INFO, "system": "Linux", "macos_version": None}
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "unsupported_platform")
        self.assertEqual(result["errors"], [])

    def test_reminders_bundle_version_comes_from_plist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))

            result = reminders_doctor.inspect_reminders_app([fixture.app])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["details"]["version"], "7.0")
        self.assertEqual(result["details"]["build"], "3976")


class ContentFreeSchemaTests(unittest.TestCase):
    def test_database_inspection_does_not_report_reminder_or_account_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))

            result = reminders_doctor.inspect_db(fixture.database)
            encoded = json.dumps(result, ensure_ascii=False)

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("SECRET REMINDER TITLE", encoded)
        self.assertNotIn("private@example.com", encoded)
        self.assertNotIn("secret-account-id.sqlite", encoded)
        self.assertFalse(result["reminder_content_rows_read"])
        self.assertFalse(result["titles_read"])
        self.assertEqual(
            result["account_metadata"]["anonymous_row_count"], 1
        )
        self.assertTrue(
            result["account_metadata"]["aggregate_count_query_performed"]
        )

    def test_database_is_opened_read_only(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def connector(database_uri: str, **kwargs: object) -> sqlite3.Connection:
            calls.append((database_uri, kwargs))
            return sqlite3.connect(database_uri, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            result = reminders_doctor.inspect_db(
                fixture.database, connector=connector
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertIn("mode=ro", calls[0][0])
        self.assertIs(calls[0][1]["uri"], True)

    def test_missing_command_column_is_reported_by_name(self) -> None:
        requirements = reminders_doctor.COMMAND_SCHEMA_REQUIREMENTS[
            "delete_attachment_db"
        ]
        schema = {table: set(columns) for table, columns in requirements.items()}
        schema["ZREMCDOBJECT"].remove("ZMARKEDFORDELETION")

        result = reminders_doctor.command_schema_capabilities(schema)[
            "delete_attachment_db"
        ]

        self.assertFalse(result["supported"])
        self.assertEqual(result["code"], "schema_mismatch")
        self.assertEqual(
            result["missing_columns"],
            {"ZREMCDOBJECT": ["ZMARKEDFORDELETION"]},
        )

    def test_store_report_uses_anonymous_refs_not_database_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))

            result = reminders_doctor.inspect_store_access(fixture.paths)
            encoded = json.dumps(result)

        self.assertIn("store_1", encoded)
        self.assertNotIn("secret-account-id.sqlite", encoded)
        self.assertFalse(
            result["details"]["stores"]["database_filenames_reported"]
        )

    def test_sqlite_failure_has_structured_error_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "not-a-database.sqlite"
            path.write_text("not sqlite", encoding="utf-8")

            result = reminders_doctor.inspect_db(path)
            encoded = json.dumps(result)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["errors"][0]["code"], "invalid_store")
        self.assertNotIn("Traceback", encoded)


class StaticDependencyTests(unittest.TestCase):
    def test_missing_developer_directory_never_invokes_clang_shim(self) -> None:
        commands: list[list[str]] = []

        def runner(
            argv: list[str], *, timeout: float = 20.0
        ) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            if argv == ["/usr/bin/xcode-select", "-p"]:
                return subprocess.CompletedProcess(
                    argv,
                    2,
                    "",
                    "xcode-select: error: unable to get active developer directory",
                )
            raise AssertionError(f"unexpected developer tool invocation: {argv}")

        def which(command: str) -> str | None:
            return {
                "xcode-select": "/usr/bin/xcode-select",
                "clang": "/usr/bin/clang",
            }.get(command)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            result = reminders_doctor.inspect_helper_toolchain(
                fixture.paths,
                syntax_check=True,
                which=which,
                runner=runner,
            )

        self.assertEqual(commands, [["/usr/bin/xcode-select", "-p"]])
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["code"], "developer_tools_unavailable")
        self.assertFalse(result["details"]["syntax_check"]["attempted"])
        self.assertFalse(result["details"]["developer_tools"]["install_requested"])

    def test_static_command_uses_explicit_diagnostic_output_budgets(self) -> None:
        completed = subprocess.CompletedProcess(["clang"], 0, "", "")
        with mock.patch.object(
            reminders_doctor,
            "run_bounded_process",
            return_value=completed,
        ) as run:
            result = reminders_doctor.run_static_command(["clang"], timeout=3.0)

        self.assertIs(result, completed)
        self.assertEqual(
            run.call_args.kwargs,
            {
                "timeout_s": 3.0,
                "stdout_limit": reminders_doctor.DOCTOR_STDOUT_LIMIT_BYTES,
                "stderr_limit": reminders_doctor.DOCTOR_STDERR_LIMIT_BYTES,
                "output": "utf8",
            },
        )

    def test_static_process_failures_keep_stable_doctor_codes(self) -> None:
        failures = (
            (
                reminders_doctor.ProcessLaunchError(
                    argv=("clang",),
                    cause=FileNotFoundError("synthetic launch failure"),
                ),
                "clang_invocation_failed",
            ),
            (
                reminders_doctor.ProcessError(
                    "synthetic post-launch failure",
                    argv=("clang",),
                    pid=123,
                    returncode=-15,
                    stdout=b"",
                    stderr=b"",
                ),
                "helper_syntax_check_failed",
            ),
        )
        for failure, code in failures:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                fixture = DoctorFixture(Path(temporary))

                def failing_runner(
                    argv: list[str], **__: object
                ) -> subprocess.CompletedProcess[str]:
                    if argv == ["/usr/bin/xcode-select", "-p"]:
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    raise failure

                result = reminders_doctor.inspect_helper_toolchain(
                    fixture.paths,
                    which=fake_which,
                    runner=failing_runner,
                )

            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["code"], code)

    def test_helper_buildability_uses_syntax_only_and_writes_no_executable(self) -> None:
        commands: list[list[str]] = []

        def runner(
            argv: list[str], *, timeout: float = 20.0
        ) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            result = reminders_doctor.inspect_helper_toolchain(
                fixture.paths,
                which=fake_which,
                runner=runner,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], ["/usr/bin/xcode-select", "-p"])
        self.assertIn("-fsyntax-only", commands[1])
        self.assertNotIn("-o", commands[1])
        self.assertFalse(
            result["details"]["syntax_check"]["mutating_build_attempted"]
        )
        self.assertFalse(
            result["details"]["syntax_check"]["executable_written"]
        )

    def test_helper_failure_reports_diagnostic_hash_not_text(self) -> None:
        def failing_runner(
            argv: list[str], *, timeout: float = 20.0
        ) -> subprocess.CompletedProcess[str]:
            if argv == ["/usr/bin/xcode-select", "-p"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "PRIVATE DIAGNOSTIC")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            result = reminders_doctor.inspect_helper_toolchain(
                fixture.paths,
                which=fake_which,
                runner=failing_runner,
            )
            encoded = json.dumps(result)

        self.assertEqual(result["code"], "helper_syntax_check_failed")
        self.assertNotIn("PRIVATE DIAGNOSTIC", encoded)
        self.assertFalse(
            result["details"]["syntax_check"]["diagnostic_text_reported"]
        )

    def test_private_framework_check_never_loads_framework(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))

            result = reminders_doctor.inspect_private_frameworks(fixture.paths)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["details"]["check_mode"], "filesystem_metadata_only")
        self.assertFalse(result["details"]["dlopen_attempted"])
        self.assertFalse(result["details"]["classes_instantiated"])


class PermissionAndAccountTests(unittest.TestCase):
    def test_eventkit_and_reminders_are_not_invoked(self) -> None:
        commands: list[list[str]] = []

        def runner(
            argv: list[str], *, timeout: float = 20.0
        ) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            report = reminders_doctor.collect_report(
                fixture.paths,
                system_info=DARWIN_INFO,
                which=fake_which,
                runner=runner,
            )

        self.assertEqual(commands, [])
        permissions = report["checks"]["permissions"]["details"]
        self.assertNotIn("automation", permissions)
        self.assertEqual(permissions["reminders"]["status"], "unknown")
        self.assertFalse(permissions["tcc_prompt_attempted"])
        self.assertFalse(report["privacy"]["permission_prompt_attempted"])
        self.assertNotIn("applescript_operations", report["capabilities"])

    def test_account_visibility_reports_counts_without_names_or_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            stores = reminders_doctor.inspect_store_access(fixture.paths)

            result = reminders_doctor.inspect_account_visibility(
                fixture.paths, stores
            )
            encoded = json.dumps(result)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["details"]["account_metadata"]["anonymous_row_count_sum"],
            1,
        )
        self.assertNotIn("private@example.com", encoded)
        self.assertNotIn("DO-NOT-REPORT", encoded)
        self.assertEqual(
            result["details"]["icloud_sync"]["status"], "unknown"
        )


class LocalArtifactTests(unittest.TestCase):
    def test_artifact_report_uses_metadata_and_redacted_paths_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            fixture.make_artifacts_private()

            result = reminders_doctor.inspect_local_artifacts(fixture.paths)
            encoded = json.dumps(result)

        self.assertEqual(result["status"], "ok")
        self.assertNotIn(str(fixture.home), encoded)
        self.assertNotIn("NEVER REPORT THIS", encoded)
        self.assertNotIn("CACHE SECRET", encoded)
        self.assertNotIn("sensitive-backup.tgz", encoded)
        journal = result["details"]["artifacts"]["journal"]
        self.assertTrue(journal["path"].startswith("~/"))
        self.assertEqual(journal["actual_mode"], "0o600")
        self.assertFalse(journal["content_read"])

    def test_broad_permissions_are_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            fixture.paths["cache_dir"].mkdir(mode=0o755, parents=True)
            os.chmod(fixture.paths["cache_dir"], 0o755)

            result = reminders_doctor.inspect_local_artifacts(fixture.paths)

        self.assertEqual(result["status"], "warning")
        self.assertIn(
            "cache_directory", result["details"]["warning_artifacts"]
        )

    def test_redaction_contract_is_static_and_does_not_read_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            fixture.make_artifacts_private()

            result = reminders_doctor.inspect_redaction_contract(fixture.paths)
            encoded = json.dumps(result)

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["details"]["journal_content_inspected"])
        self.assertNotIn("NEVER REPORT THIS", encoded)


class ReportContractTests(unittest.TestCase):
    def test_default_report_declares_runtime_boundaries_without_processes(self) -> None:
        commands: list[list[str]] = []

        def forbidden_runner(
            argv: list[str], *, timeout: float = 20.0
        ) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            raise AssertionError(f"metadata-only Doctor executed a process: {argv}")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            report = reminders_doctor.collect_report(
                fixture.paths,
                system_info=DARWIN_INFO,
                which=fake_which,
                runner=forbidden_runner,
            )

        self.assertEqual(commands, [])
        self.assertEqual(
            report["execution"],
            {
                "mode": "metadata_only",
                "developer_tool_process_attempted": False,
                "compiler_process_attempted": False,
                "install_request_attempted": False,
            },
        )
        boundaries = report["capabilities"]["runtime_boundaries"]
        self.assertEqual(
            boundaries["core"],
            {
                "maturity": "stable",
                "requires_command_line_tools": False,
                "compiler_invocation": "never",
                "paths": ["core"],
            },
        )
        self.assertEqual(
            boundaries["compiler_free_private"]["paths"],
            [
                "tag_mutation",
                "url_only_attachment_mutation",
                "read_only_native_inspection",
            ],
        )
        self.assertFalse(
            boundaries["compiler_free_private"]["requires_command_line_tools"]
        )
        self.assertEqual(
            boundaries["compiler_required_private"]["paths"],
            [
                "section_mutation",
                "image_attachment_mutation",
                "exact_recently_deleted",
            ],
        )
        self.assertTrue(
            boundaries["compiler_required_private"]["requires_command_line_tools"]
        )

    def test_missing_canonical_framework_paths_require_a_runtime_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            fixture.paths["private_frameworks"] = {
                "ReminderKit": fixture.root
                / "missing/ReminderKit.framework/ReminderKit",
                "ReminderKitInternal": fixture.root
                / "missing/ReminderKitInternal.framework/ReminderKitInternal",
            }

            report = reminders_doctor.collect_report(
                fixture.paths,
                system_info=DARWIN_INFO,
                which=fake_which,
                runner=fake_runner,
            )

        capability = report["capabilities"]["reminderkit_image_attachments"]
        self.assertEqual(
            report["checks"]["private_frameworks"]["code"],
            "private_framework_unavailable",
        )
        self.assertEqual(capability["status"], "unknown")
        self.assertEqual(
            capability["basis"],
            "canonical_framework_paths_absent_runtime_probe_required",
        )
        self.assertTrue(capability["requires_runtime_verification"])

    def test_ready_report_has_stable_privacy_and_capability_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            fixture.make_artifacts_private()

            report = reminders_doctor.collect_report(
                fixture.paths,
                system_info=DARWIN_INFO,
                which=fake_which,
                runner=fake_runner,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["privacy"]["content_free"])
        self.assertFalse(report["privacy"]["write_attempted"])
        self.assertFalse(report["privacy"]["application_launched"])
        command_schema = report["capabilities"]["command_schema"]
        self.assertIn("delete_attachment_db", command_schema)
        self.assertIn("add_tag_db", command_schema)
        self.assertTrue(
            {
                "audit_attachments",
                "cache_rebuild",
                "cleanup_tags",
                "complete_reminder_db",
                "create_list_db",
                "create_reminder_db",
                "delete_reminder_db",
                "list_lists",
                "repair_attachments",
                "search_reminders",
                "snapshot",
                "update_reminder_db",
            }.isdisjoint(command_schema)
        )
        self.assertEqual(
            report["capabilities"]["reminderkit_image_attachments"],
            {
                "status": "unknown",
                "basis": "toolchain_metadata_only_not_probed",
                "requires_runtime_verification": True,
            },
        )
        self.assertEqual(report["errors"], [])

    def test_helper_failure_keeps_reminderkit_capability_blocked(self) -> None:
        def failing_runner(
            argv: list[str], *, timeout: float = 20.0
        ) -> subprocess.CompletedProcess[str]:
            if argv == ["/usr/bin/xcode-select", "-p"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "compile failure")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            report = reminders_doctor.collect_report(
                fixture.paths,
                system_info=DARWIN_INFO,
                syntax_check=True,
                which=fake_which,
                runner=failing_runner,
            )

        self.assertEqual(
            report["capabilities"]["reminderkit_image_attachments"],
            {
                "status": "blocked",
                "basis": "static_prerequisites_failed",
                "requires_runtime_verification": True,
            },
        )

    def test_missing_group_produces_blocked_report_and_structured_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DoctorFixture(Path(temporary))
            fixture.database.unlink()
            fixture.paths["stores"].rmdir()
            fixture.paths["files"].joinpath("Account-DO-NOT-REPORT").rmdir()
            fixture.paths["files"].rmdir()
            fixture.paths["group"].rmdir()

            report = reminders_doctor.collect_report(
                fixture.paths,
                system_info=DARWIN_INFO,
                which=fake_which,
                runner=fake_runner,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            report["checks"]["store_access"]["code"],
            "group_container_missing",
        )
        self.assertEqual(
            report["checks"]["permissions"]["details"]["full_disk_access"][
                "status"
            ],
            "unknown",
        )

    def test_main_returns_nonzero_for_blocked_gate(self) -> None:
        blocked = {
            "schema_version": 1,
            "doctor": "test",
            "ok": False,
            "status": "blocked",
        }
        with mock.patch.object(
            reminders_doctor, "collect_report", return_value=blocked
        ), mock.patch.object(reminders_doctor.sys, "stdout") as stdout:
            stdout.write = mock.Mock()
            code = reminders_doctor.main(["--compact"])

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
