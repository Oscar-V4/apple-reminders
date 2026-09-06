from __future__ import annotations

from contextlib import closing, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import section_acceptance_plan as plan


def helper_binding():
    return {"plugin_version": "0.7.0", "source_commit": "a" * 40, "workflow_commit": "b" * 40,
            "source_files": {name: "c" * 64 for name in plan.native_helper.SOURCES},
            "binary_sha256": {name: "d" * 64 for name in plan.native_helper.EXECUTABLES},
            "manifest_sha256": "e" * 64}


class SectionPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = helper_binding()
        self.plan = plan.make_plan("EXPLICIT-ACCOUNT", self.helper)

    def snapshot(self):
        path = self.root / "schema.sqlite"
        tables = {}
        for contract in self.plan["contracts"].values():
            for table, columns in contract["required_tables"].items():
                tables.setdefault(table, set()).update(columns)
        with closing(sqlite3.connect(path)) as connection, connection:
            for table, columns in tables.items():
                connection.execute(f'CREATE TABLE "{table}" ({", ".join(sorted(columns))})')
            connection.execute("CREATE TABLE PRIVATE_ROWS (secret TEXT)")
            connection.execute("INSERT INTO PRIVATE_ROWS VALUES ('sensitive reminder content')")
        return path

    def test_default_cli_plans_without_runtime_or_database_observation(self):
        output = io.StringIO()
        with mock.patch.object(plan, "verified_helper_binding", return_value=self.helper), \
             mock.patch.object(plan, "observed_runtime", side_effect=AssertionError("runtime observed")), \
             mock.patch.object(plan.sqlite3, "connect", side_effect=AssertionError("database opened")), redirect_stdout(output):
            self.assertEqual(plan.main(["--account-id", "EXPLICIT-ACCOUNT"]), 0)
        value = json.loads(output.getvalue())
        plan.validate_plan(value, self.helper)
        self.assertFalse(value["runtime_observed"])
        self.assertFalse(value["account_selection"]["ownership_verified"])
        self.assertFalse(value["mutation_authority"])
        self.assertEqual(value["object_registry"], {"list_ids": [], "reminder_ids": [], "section_ids": []})
        self.assertNotEqual(value["nonce"], self.plan["nonce"])

    def test_bad_plans_fail_before_opening_snapshot(self):
        faults = [
            lambda p: p["object_registry"]["list_ids"].append("ARBITRARY-LIST"),
            lambda p: p["object_registry"]["reminder_ids"].append("ARBITRARY-REMINDER"),
            lambda p: p["object_registry"]["section_ids"].append("ARBITRARY-SECTION"),
            lambda p: p.update(permitted_operation="move_to_section"),
            lambda p: p.update(mutation_authority=True),
            lambda p: p.update(runtime_observed=True),
            lambda p: p["account_selection"].update(ownership_verified=True),
            lambda p: p.update(nonce="not-a-nonce"),
            lambda p: p.update(unbounded=True),
            lambda p: p["helper_binding"].update(source_commit="f" * 40),
            lambda p: p["contracts"]["create_section_db"].update(required_tables={}),
            lambda p: p.pop("account_selection"),
            lambda p: p.update(schema_version=True),
            lambda p: p.update(mutation_authority=0),
        ]
        for fault in faults:
            value = deepcopy(self.plan)
            fault(value)
            with self.subTest(fault=fault), mock.patch.object(plan.sqlite3, "connect", side_effect=AssertionError("database opened")):
                with self.assertRaises(plan.PlanError):
                    plan.collect_schema(value, self.root / "missing", helper=self.helper, runtime=plan.EXPECTED_RUNTIME)

    def test_account_and_file_inputs_are_bounded_and_explicit(self):
        for value in (None, "", "*", "all accounts", "a" * 257, "line\nbreak"):
            with self.subTest(value=value), self.assertRaises(plan.PlanError):
                plan.make_plan(value, self.helper)
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b" " * (plan.MAX_JSON_BYTES + 1))
        with self.assertRaises(plan.PlanError):
            plan.read_json(oversized)
        symlink = self.root / "link.json"
        symlink.symlink_to(oversized)
        with self.assertRaises(plan.PlanError):
            plan.read_json(symlink)
        with self.assertRaises(plan.PlanError):
            plan.read_json(Path("relative.json"))

    def test_snapshot_collection_reads_fixed_schema_only_and_never_claims_admission(self):
        snapshot = self.snapshot()
        before = snapshot.read_bytes()
        queries = []
        real_connect = sqlite3.connect

        class RecordingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                queries.append(sql)
                return super().execute(sql, *args, **kwargs)

        def connect(sqlite_uri, **kwargs):
            self.assertIn("mode=ro&immutable=1", sqlite_uri)
            return real_connect(sqlite_uri, factory=RecordingConnection, **kwargs)

        with mock.patch.object(plan.sqlite3, "connect", side_effect=connect):
            evidence = plan.collect_schema(self.plan, snapshot, helper=self.helper, runtime=dict(plan.EXPECTED_RUNTIME))
        self.assertTrue(queries)
        self.assertTrue(all(query.startswith('PRAGMA table_info("') for query in queries))
        self.assertNotIn("PRIVATE_ROWS", " ".join(queries))
        self.assertNotIn("sensitive reminder content", json.dumps(evidence))
        self.assertTrue(all(item["minimum_fields_present"] for item in evidence["observations"].values()))
        for key in ("snapshot_origin_verified", "account_ownership_verified", "reminder_rows_read", "account_rows_read", "mutation_attempted", "runtime_admission"):
            self.assertIs(evidence[key], False)
        self.assertEqual(snapshot.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), {snapshot})
        self.assertEqual(evidence["plan_sha256"], plan.digest(self.plan))

    def test_wrong_runtime_and_wal_snapshots_stop_before_sqlite(self):
        snapshot = self.snapshot()
        with mock.patch.object(plan.sqlite3, "connect", side_effect=AssertionError("database opened")):
            with self.assertRaises(plan.PlanError):
                plan.collect_schema(self.plan, snapshot, helper=self.helper, runtime={**plan.EXPECTED_RUNTIME, "macos_build": "unknown"})
            Path(str(snapshot) + "-wal").write_bytes(b"snapshot not standalone")
            with self.assertRaises(plan.PlanError):
                plan.collect_schema(self.plan, snapshot, helper=self.helper, runtime=plan.EXPECTED_RUNTIME)

    def test_missing_schema_is_evidence_not_promotion(self):
        snapshot = self.root / "empty.sqlite"
        sqlite3.connect(snapshot).close()
        evidence = plan.collect_schema(self.plan, snapshot, helper=self.helper, runtime=plan.EXPECTED_RUNTIME)
        self.assertTrue(all(not item["minimum_fields_present"] for item in evidence["observations"].values()))
        self.assertFalse(evidence["runtime_admission"])

    def test_oversized_schema_is_rejected_and_views_expose_only_metadata(self):
        for kind in ("too_many_columns", "view_reads_content"):
            with self.subTest(kind=kind):
                snapshot = self.root / f"{kind}.sqlite"
                table = next(iter(self.plan["contracts"]["create_section_db"]["required_tables"]))
                with closing(sqlite3.connect(snapshot)) as connection, connection:
                    if kind == "too_many_columns":
                        columns = ",".join(f"COLUMN_{n}" for n in range(plan.MAX_COLUMNS + 1))
                        connection.execute(f'CREATE TABLE "{table}" ({columns})')
                    else:
                        connection.execute("CREATE TABLE PRIVATE_ROWS (secret TEXT)")
                        connection.execute("INSERT INTO PRIVATE_ROWS VALUES ('private content must not be read')")
                        connection.execute(f'CREATE VIEW "{table}" AS SELECT secret FROM PRIVATE_ROWS')
                if kind == "too_many_columns":
                    with self.assertRaises(plan.PlanError):
                        plan.collect_schema(self.plan, snapshot, helper=self.helper, runtime=plan.EXPECTED_RUNTIME)
                else:
                    evidence = plan.collect_schema(self.plan, snapshot, helper=self.helper, runtime=plan.EXPECTED_RUNTIME)
                    self.assertNotIn("private content must not be read", json.dumps(evidence))
                    self.assertFalse(evidence["observations"]["create_section_db"]["minimum_fields_present"])

    def test_unverified_helper_cannot_fall_back_to_store_or_source_build(self):
        with mock.patch.object(plan.native_helper, "resolve_helper", side_effect=plan.native_helper.NativeHelperUnavailable("unverified")), \
             mock.patch.object(plan, "observed_runtime", side_effect=AssertionError("runtime observed")), \
             mock.patch.object(plan.sqlite3, "connect", side_effect=AssertionError("database opened")), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(plan.main(["--account-id", "EXPLICIT-ACCOUNT"]), 1)
        self.assertEqual(output.getvalue(), "")

    def test_cli_collection_requires_explicit_inputs_before_any_probes(self):
        for argv in ([], ["--collect-schema"], ["--account-id", "EXPLICIT", "--schema-snapshot", "/tmp/test"]):
            with self.subTest(argv=argv), mock.patch.object(plan, "verified_helper_binding", side_effect=AssertionError("helper inspected")), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    plan.main(argv)

    def test_collector_is_repository_only(self):
        import audit_source_package
        self.assertNotIn(Path("scripts/section_acceptance_plan.py"), audit_source_package.PACKAGE_ROOT_FILES)
        self.assertFalse((ROOT / "plugins/apple-reminders/scripts/section_acceptance_plan.py").exists())


if __name__ == "__main__":
    unittest.main()
