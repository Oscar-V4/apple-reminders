from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
ADAPTER_PATH = PLUGIN_ROOT / "scripts" / "reminders_adapter.py"
SPEC = importlib.util.spec_from_file_location("reminders_adapter_contract_tests", ADAPTER_PATH)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)

import durable_idempotency  # noqa: E402
from receipt_contract import (  # noqa: E402
    AdapterError,
    MutationNotStartedError,
    adapter_receipt_error,
    failed_no_mutation_evidence_error,
    validated_receipt_mutation_state,
)


class ReceiptAndErrorContractTests(unittest.TestCase):
    def test_validated_receipt_state_is_evidence_aware(self) -> None:
        cases = (
            ({"status": "unchanged"}, "not_mutated"),
            (
                {
                    "status": "unchanged",
                    "verification": {"write_performed": False},
                },
                "not_mutated",
            ),
            (
                {
                    "status": "unchanged",
                    "verification": {"write_performed": True},
                },
                "unknown",
            ),
            ({"status": "unchanged", "committed": True}, "unknown"),
            ({"status": "verified"}, "committed"),
            (
                {
                    "status": "verified",
                    "verification": {"write_performed": False},
                },
                "unknown",
            ),
            (
                {
                    "status": "partial_success",
                    "verification": {"write_performed": True},
                },
                "committed",
            ),
            ({"status": "partial_success"}, "unknown"),
        )
        for receipt, expected in cases:
            with self.subTest(receipt=receipt):
                self.assertEqual(
                    validated_receipt_mutation_state(receipt),
                    expected,
                )

    def test_operation_receipt_has_stable_shape(self) -> None:
        payload = adapter.operation_receipt(
            status="verified",
            operation="update_reminder",
            backend="db",
            target={"id": "R-1"},
            verification={"state": "read_back"},
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(payload["operation"], "update_reminder")
        self.assertTrue(payload["operation_id"])
        self.assertEqual(payload["target"], {"id": "R-1"})

    def test_adapter_reexports_shared_error_types(self) -> None:
        self.assertIs(adapter.AdapterError, AdapterError)
        self.assertIs(adapter.MutationNotStartedError, MutationNotStartedError)
        self.assertIs(
            adapter.execute_idempotent,
            durable_idempotency.execute_idempotent,
        )

    def test_exact_reminder_selector_rejects_unbounded_or_title_only_delete(self) -> None:
        with self.assertRaises(adapter.AdapterError) as empty:
            adapter.require_exact_reminder_selector(
                reminder_id=None,
                title=None,
                list_name=None,
            )
        with self.assertRaises(adapter.AdapterError) as title_only:
            adapter.require_exact_reminder_selector(
                reminder_id=None,
                title="Only title",
                list_name=None,
            )

        self.assertEqual(empty.exception.code, "ambiguous_target")
        self.assertEqual(title_only.exception.code, "ambiguous_target")

    def test_adapter_error_is_rendered_as_machine_readable_error(self) -> None:
        with mock.patch.object(adapter, "json_out") as output:
            code = adapter.fail(
                "Changed",
                code="concurrent_modification",
                current_version=3,
            )

        self.assertEqual(code, 1)
        payload = output.call_args.args[0]
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertEqual(payload["error"]["code"], "concurrent_modification")
        self.assertEqual(payload["error"]["current_version"], 3)

    def test_uncompensated_partial_failure_requires_manual_repair(self) -> None:
        args = argparse.Namespace(
            func=mock.Mock(
                side_effect=adapter.AdapterError(
                    "Replacement failed",
                    partial_failure=True,
                    compensated=False,
                    new_attachment_id="A-1",
                )
            )
        )
        parser = mock.Mock()
        parser.parse_args.return_value = args

        with (
            mock.patch.object(adapter, "build_parser", return_value=parser),
            mock.patch.object(adapter, "json_out") as output,
        ):
            code = adapter.main([])

        self.assertEqual(code, 1)
        payload = output.call_args.args[0]
        self.assertEqual(payload["status"], "failed_manual_repair_required")
        self.assertEqual(payload["error"]["new_attachment_id"], "A-1")

    def test_failed_no_mutation_requires_affirmative_no_write_evidence(self) -> None:
        receipt = adapter.operation_receipt(
            status="failed_no_mutation",
            operation="attach_url",
            backend="sqlite_private",
            target={"reminder_id": "R-1"},
            after={},
            verification={
                "state": "not_performed",
                "write_performed": False,
                "final_read": False,
            },
            recovery={"semantics": "fix_input"},
        )
        receipt["error"] = {
            "code": "invalid_input",
            "message": "The write was rejected before dispatch.",
        }

        self.assertIsNone(failed_no_mutation_evidence_error(receipt))
        self.assertIsNone(
            adapter_receipt_error(receipt, expected_operation="attach_url")
        )

        contradictions = (
            lambda value: value["verification"].__setitem__("write_performed", True),
            lambda value: value.__setitem__("after", {"attachment": {"id": "A-1"}}),
            lambda value: value.__setitem__("mutation_attempted", True),
        )
        for contradict in contradictions:
            with self.subTest(contradiction=contradict):
                unsafe = json.loads(json.dumps(receipt))
                contradict(unsafe)
                self.assertIsNotNone(failed_no_mutation_evidence_error(unsafe))
                self.assertIsNotNone(
                    adapter_receipt_error(
                        unsafe,
                        expected_operation="attach_url",
                    )
                )
                self.assertEqual(
                    validated_receipt_mutation_state(unsafe),
                    "unknown",
                )


class JournalPrivacyTests(unittest.TestCase):
    def test_sensitive_action_fields_are_redacted(self) -> None:
        raw = {
            "id": "R-1",
            "title": "Private health reminder",
            "list": "Medical",
            "url": "https://secret.example/path",
            "source_path": "/Users/example/private.png",
            "nested": {"filename": "private.png", "count": 2},
        }

        redacted = adapter.redact_log_payload(raw)
        encoded = json.dumps(redacted, ensure_ascii=False)

        self.assertEqual(redacted["id"], "R-1")
        self.assertTrue(redacted["title"]["redacted"])
        self.assertTrue(redacted["nested"]["filename"]["redacted"])
        self.assertNotIn("Private health reminder", encoded)
        self.assertNotIn("secret.example", encoded)
        self.assertNotIn("private.png", encoded)

    def test_journal_failure_is_returned_as_warning_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            journal = support / "actions.jsonl"
            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "JOURNAL", journal),
                mock.patch.object(Path, "open", side_effect=OSError("denied")),
            ):
                warning = adapter.log_action("create_reminder", {"title": "Private"})

        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], "journal_write_failed")


if __name__ == "__main__":
    unittest.main()
