from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "scripts" / "validate_external_tester_receipt.py"
EXAMPLE = (
    REPO_ROOT
    / "docs"
    / "launch"
    / "examples"
    / "external-tester-receipt.example.json"
)
SCHEMA = REPO_ROOT / "docs" / "launch" / "external-tester-receipt.schema.json"


class ExternalTesterReceiptTests(unittest.TestCase):
    def run_bytes(self, payload: bytes) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "receipt.json"
            path.write_bytes(payload)
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(path)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def run_text(self, text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "receipt.json"
            path.write_text(text, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(path)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def run_payload(self, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return self.run_text(json.dumps(payload))

    def example_payload(self) -> dict[str, object]:
        return json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def checks(
        self, *values: tuple[str, str, str]
    ) -> list[dict[str, str]]:
        return [
            {"id": check_id, "outcome": outcome, "error_category": category}
            for check_id, outcome, category in values
        ]

    def test_checked_in_redacted_example_validates_through_the_cli(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(VALIDATOR), str(EXAMPLE)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "receipt 1: valid\n")
        self.assertEqual(completed.stderr, "")

    def test_cleanup_summary_cannot_disagree_with_the_cleanup_check(self) -> None:
        payload = self.example_payload()
        checks = payload["checks"]
        self.assertIsInstance(checks, list)
        cleanup = next(check for check in checks if check["id"] == "exact_cleanup")
        cleanup["outcome"] = "failed"
        cleanup["error_category"] = "cleanup_failed"

        completed = self.run_payload(payload)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "receipt 1: invalid (scenario_error)\n")

    def test_unknown_personal_field_is_rejected_without_echoing_its_value(self) -> None:
        payload = self.example_payload()
        private_value = "Private dental appointment"
        payload["reminder_title"] = private_value

        completed = self.run_payload(payload)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "receipt 1: invalid (privacy_error)\n")
        self.assertNotIn(private_value, completed.stderr)

    def test_intel_receipt_requires_synthetic_crud_and_cleanup_checks(self) -> None:
        payload = self.example_payload()
        payload.update(
            {
                "hardware": "intel",
                "scenario": "intel_core",
                "tcc_precondition": "granted",
                "tcc_result": "granted_without_prompt",
                "checks": self.checks(
                    ("install", "passed", "none"),
                    ("core_bounded_read", "passed", "none"),
                ),
                "exact_cleanup": "not_run",
            }
        )

        completed = self.run_payload(payload)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "receipt 1: invalid (scenario_error)\n")

    def test_cleanup_cannot_be_claimed_without_a_cleanup_check(self) -> None:
        payload = self.example_payload()
        payload.update(
            {
                "scenario": "upgrade_identity",
                "tcc_precondition": "granted",
                "tcc_result": "granted_without_prompt",
                "checks": self.checks(
                    ("install", "passed", "none"),
                    ("release_verification", "passed", "none"),
                    ("upgrade_identity", "passed", "none"),
                    ("core_bounded_read", "passed", "none"),
                    ("core_canonical_alarm", "passed", "none"),
                ),
                "exact_cleanup": "passed",
            }
        )

        completed = self.run_payload(payload)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "receipt 1: invalid (scenario_error)\n")

    def test_each_documented_scenario_has_a_valid_closed_shape(self) -> None:
        passed = ("passed", "none")
        cases: dict[str, dict[str, object]] = {
            "fresh_core_allow": {},
            "fresh_core_deny": {
                "scenario": "fresh_core_deny",
                "tcc_result": "denied",
                "checks": self.checks(
                    ("install", *passed),
                    ("release_verification", *passed),
                    ("permission_deny_no_retry", "blocked", "permission_denied"),
                ),
                "exact_cleanup": "not_run",
            },
            "intel_core": {
                "hardware": "intel",
                "scenario": "intel_core",
                "tcc_precondition": "granted",
                "tcc_result": "granted_without_prompt",
                "checks": self.checks(
                    ("install", *passed),
                    ("release_verification", *passed),
                    ("core_bounded_read", *passed),
                    ("core_synthetic_crud", *passed),
                    ("core_canonical_alarm", *passed),
                    ("exact_cleanup", *passed),
                ),
            },
            "minimum_macos_core": {
                "macos_version": "14.0",
                "scenario": "minimum_macos_core",
                "tcc_precondition": "granted",
                "tcc_result": "granted_without_prompt",
                "checks": self.checks(
                    ("install", *passed),
                    ("release_verification", *passed),
                    ("core_bounded_read", *passed),
                    ("core_synthetic_crud", *passed),
                    ("core_canonical_alarm", *passed),
                    ("exact_cleanup", *passed),
                ),
            },
            "upgrade_identity": {
                "scenario": "upgrade_identity",
                "tcc_precondition": "granted",
                "tcc_result": "granted_without_prompt",
                "checks": self.checks(
                    ("install", *passed),
                    ("release_verification", *passed),
                    ("upgrade_identity", *passed),
                    ("core_bounded_read", *passed),
                    ("core_canonical_alarm", *passed),
                    ("exact_cleanup", *passed),
                ),
            },
            "clt_only_experimental": {
                "command_line_tools": "installed",
                "scenario": "clt_only_experimental",
                "tcc_precondition": "granted",
                "tcc_result": "granted_without_prompt",
                "checks": self.checks(
                    ("install", *passed),
                    ("release_verification", *passed),
                    ("core_bounded_read", *passed),
                    ("experimental_capability", *passed),
                    ("experimental_synthetic_mutation", *passed),
                    ("exact_cleanup", *passed),
                ),
            },
        }

        for scenario, updates in cases.items():
            with self.subTest(scenario=scenario):
                payload = self.example_payload()
                payload.update(updates)

                completed = self.run_payload(payload)

                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_every_scenario_requires_release_verification(self) -> None:
        payload = self.example_payload()
        checks = payload["checks"]
        self.assertIsInstance(checks, list)
        payload["checks"] = [
            check for check in checks if check["id"] != "release_verification"
        ]

        completed = self.run_payload(payload)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "receipt 1: invalid (scenario_error)\n")

    def test_core_mutation_scenario_requires_canonical_alarm_evidence(self) -> None:
        payload = self.example_payload()
        checks = payload["checks"]
        self.assertIsInstance(checks, list)
        payload["checks"] = [
            check for check in checks if check["id"] != "core_canonical_alarm"
        ]

        completed = self.run_payload(payload)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "receipt 1: invalid (scenario_error)\n")

    def test_raw_error_or_diagnostic_text_is_not_a_receipt_category(self) -> None:
        payload = self.example_payload()
        private_value = "EKErrorDomain /Users/person/private.log"
        checks = payload["checks"]
        self.assertIsInstance(checks, list)
        checks[0]["outcome"] = "failed"
        checks[0]["error_category"] = private_value

        completed = self.run_payload(payload)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "receipt 1: invalid (schema_error)\n")
        self.assertNotIn(private_value, completed.stderr)

    def test_json_boolean_cannot_impersonate_integer_schema_version(self) -> None:
        payload = self.example_payload()
        payload["schema_version"] = True

        completed = self.run_payload(payload)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "receipt 1: invalid (schema_error)\n")

    def test_duplicate_json_keys_are_rejected(self) -> None:
        raw = EXAMPLE.read_text(encoding="utf-8").replace(
            '"schema_version": 1,',
            '"schema_version": 1,\n  "schema_version": 1,',
            1,
        )

        completed = self.run_text(raw)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "receipt 1: invalid (invalid_json)\n")

    def test_invalid_utf8_is_rejected_without_a_path_or_traceback(self) -> None:
        completed = self.run_bytes(b"\xffprivate")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "receipt 1: invalid (invalid_json)\n")

    def test_schema_has_no_free_form_personal_data_field(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            set(schema["properties"]),
            {
                "schema_version",
                "plugin_ref",
                "codex_version",
                "hardware",
                "macos_version",
                "python",
                "xcode",
                "command_line_tools",
                "scenario",
                "tcc_precondition",
                "tcc_result",
                "checks",
                "synthetic_data_only",
                "exact_cleanup",
            },
        )

        def assert_bounded_strings(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "string":
                    self.assertTrue("enum" in node or "pattern" in node)
                    if "pattern" in node:
                        self.assertIn("maxLength", node)
                for value in node.values():
                    assert_bounded_strings(value)
            elif isinstance(node, list):
                for value in node:
                    assert_bounded_strings(value)

        assert_bounded_strings(schema["properties"])

        error_categories = set(
            schema["properties"]["checks"]["items"]["properties"]
            ["error_category"]["enum"]
        )
        self.assertTrue(
            {
                "runtime_unverified",
                "unsupported_build",
                "compiler_required",
                "schema_unverified",
                "schema_fingerprint_mismatch",
                "unsupported_capability",
                "schema_mismatch",
            }.issubset(error_categories)
        )


if __name__ == "__main__":
    unittest.main()
