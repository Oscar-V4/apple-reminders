from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "apple-reminders" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import eventkit_bridge  # noqa: E402
import eventkit_protocol  # noqa: E402


class EventKitProtocolTests(unittest.TestCase):
    @staticmethod
    def mutation_fixture(operation: str, status: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "ok": status != "failed_no_mutation",
            "status": status,
            "operation": operation,
            "operation_id": "B6A66F8E-B44F-426F-95CD-3A678865F793",
            "backend": "eventkit_public_sdk",
            "target": {"id": "REMINDER-1", "calendar_id": "CALENDAR-1"},
            "after": {"id": "REMINDER-1", "title": "After"},
            "verification": {
                "state": (
                    "pending"
                    if status == "committed_verification_pending"
                    else "read_back"
                ),
                "matched": status != "committed_verification_pending",
            },
            "recovery": {"semantics": "eventkit_native_api"},
        }
        if operation != "create_reminder":
            payload["before"] = {"id": "REMINDER-1", "title": "Before"}
        if status == "committed_verification_pending":
            payload["warnings"] = [
                {
                    "code": "verification_pending",
                    "message": "Read back before retrying.",
                }
            ]
            payload["error"] = {
                "code": "sync_pending",
                "reason_code": "committed_verification_mismatch",
                "message": "Committed; verification pending",
            }
        return payload

    def test_public_interface_has_exactly_three_entry_points(self) -> None:
        self.assertEqual(
            eventkit_protocol.__all__,
            [
                "validate_response",
                "validate_mutation_receipt",
                "mutation_outcome_unknown_response",
            ],
        )

    def test_bridge_directly_reexports_protocol_functions_and_constants(self) -> None:
        self.assertIs(
            eventkit_bridge.validate_response,
            eventkit_protocol.validate_response,
        )
        self.assertIs(
            eventkit_bridge.mutation_outcome_unknown_response,
            eventkit_protocol.mutation_outcome_unknown_response,
        )
        self.assertIs(
            eventkit_bridge.validate_mutation_receipt,
            eventkit_protocol.validate_mutation_receipt,
        )
        self.assertIs(eventkit_bridge.SCHEMA_VERSION, eventkit_protocol.SCHEMA_VERSION)
        self.assertIs(
            eventkit_bridge.MUTATION_OPERATIONS,
            eventkit_protocol.MUTATION_OPERATIONS,
        )
        self.assertIs(eventkit_bridge.EXIT_CODES, eventkit_protocol.EXIT_CODES)
        self.assertIs(
            eventkit_bridge.STABLE_ERROR_CODES,
            eventkit_protocol.STABLE_ERROR_CODES,
        )

    def test_unknown_mutation_outcome_has_the_exact_pending_receipt_shape(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "update_reminder",
            "reminder_id": "REMINDER-1",
            "calendar_id": "CALENDAR-1",
            "patch": {"title": "Private title"},
        }
        operation_id = uuid.UUID("12345678-1234-4234-9234-1234567890ab")

        with mock.patch.object(eventkit_protocol.uuid, "uuid4", return_value=operation_id):
            payload = eventkit_protocol.mutation_outcome_unknown_response(
                request,
                reason_code="native_timeout",
                message="EventKit operation exceeded 70 seconds",
                details={"timeout_seconds": 70},
            )

        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "operation": "update_reminder",
                "status": "committed_verification_pending",
                "ok": True,
                "operation_id": "12345678-1234-4234-9234-1234567890AB",
                "backend": "eventkit_public_sdk",
                "target": {
                    "reminder_id": "REMINDER-1",
                    "calendar_id": "CALENDAR-1",
                },
                "before": {},
                "after": {},
                "verification": {
                    "state": "pending",
                    "write_performed": None,
                    "reason_code": "native_timeout",
                },
                "recovery": {
                    "semantics": "read_before_retry",
                    "automatic_retry_safe": False,
                },
                "warnings": [
                    {
                        "code": "verification_pending",
                        "message": (
                            "The native process may have committed; read the target "
                            "before retrying."
                        ),
                    }
                ],
                "error": {
                    "code": "sync_pending",
                    "reason_code": "native_timeout",
                    "message": "EventKit operation exceeded 70 seconds",
                    "details": {"timeout_seconds": 70},
                },
            },
        )
        self.assertIs(
            eventkit_protocol.validate_response(payload, "update_reminder"),
            payload,
        )

    def test_unknown_outcome_rejects_a_read_with_the_existing_error(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "^Only EventKit mutations can have an unknown commit outcome$",
        ):
            eventkit_protocol.mutation_outcome_unknown_response(
                {"schema_version": 1, "operation": "read_reminder"},
                reason_code="native_timeout",
                message="Timed out",
            )

        for operation in ([], {}):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                ValueError,
                "^Only EventKit mutations can have an unknown commit outcome$",
            ):
                eventkit_protocol.mutation_outcome_unknown_response(
                    {"schema_version": 1, "operation": operation},
                    reason_code="native_timeout",
                    message="Timed out",
                )

    def test_unhashable_wire_status_and_error_code_use_runtime_errors(self) -> None:
        for status in ([], {}):
            with self.subTest(status=status), self.assertRaisesRegex(
                RuntimeError,
                "Native bridge returned unknown status",
            ):
                eventkit_protocol.validate_response(
                    {
                        "schema_version": 1,
                        "operation": "read_reminder",
                        "status": status,
                        "ok": False,
                    },
                    "read_reminder",
                )

        for code in ([], {}):
            with self.subTest(code=code), self.assertRaisesRegex(
                RuntimeError,
                "stable code and message",
            ):
                eventkit_protocol.validate_response(
                    {
                        "schema_version": 1,
                        "operation": "read_reminder",
                        "status": "failed_no_mutation",
                        "ok": False,
                        "error": {"code": code, "message": "Failed"},
                    },
                    "read_reminder",
                )

    def test_unhashable_no_write_evidence_is_rejected_without_type_error(
        self,
    ) -> None:
        clean = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "permission_denied",
                "message": "Full Reminders access is required",
            },
        }
        verification_cases = (
            {"write_performed": False, "final_read": [], "state": "not_performed"},
            {"write_performed": False, "final_read": False, "state": {}},
        )
        for verification in verification_cases:
            with self.subTest(verification=verification), self.assertRaisesRegex(
                RuntimeError,
                "no-mutation",
            ):
                eventkit_protocol.validate_response(
                    {**clean, "verification": verification},
                    "create_reminder",
                )

    def test_mutation_receipt_rejects_unhashable_operation_and_error_code(
        self,
    ) -> None:
        for operation in ([], {}):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                RuntimeError,
                "operation is not supported",
            ):
                eventkit_protocol.validate_mutation_receipt(
                    {"operation": operation}
                )

        for code in ([], {}):
            payload = self.mutation_fixture(
                "move_reminder",
                "committed_verification_pending",
            )
            error = payload["error"]
            self.assertIsInstance(error, dict)
            error["code"] = code
            with self.subTest(code=code), self.assertRaisesRegex(
                RuntimeError,
                "stable code and message",
            ):
                eventkit_protocol.validate_mutation_receipt(
                    payload,
                    "move_reminder",
                )

    def test_failed_no_mutation_rejects_contradictory_commit_evidence(self) -> None:
        clean = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "permission_denied",
                "reason_code": "reminders_access_denied",
                "message": "Full Reminders access is required",
                "retryable": False,
                "details": {},
            },
        }
        self.assertIs(
            eventkit_protocol.validate_response(clean, "create_reminder"),
            clean,
        )

        contradictions = (
            {"after": {"id": "REMINDER-1"}},
            {
                "verification": {
                    "state": "read_back",
                    "write_performed": True,
                    "final_read": True,
                }
            },
            {"mutation_attempted": True},
        )
        for evidence in contradictions:
            with self.subTest(evidence=evidence):
                payload = {**clean, **evidence}
                with self.assertRaisesRegex(RuntimeError, "no-mutation"):
                    eventkit_protocol.validate_response(payload, "create_reminder")

    def test_verified_create_fixture_satisfies_full_response_contract(self) -> None:
        payload = self.mutation_fixture("create_reminder", "verified")

        self.assertIs(
            eventkit_protocol.validate_response(payload, "create_reminder"),
            payload,
        )
        self.assertNotIn("before", payload)

    def test_unchanged_update_fixture_satisfies_full_response_contract(self) -> None:
        payload = self.mutation_fixture("update_reminder", "unchanged")
        payload["verification"] = {
            "state": "not_needed",
            "matched": True,
            "write_performed": False,
        }
        payload["after"] = payload["before"]

        self.assertIs(
            eventkit_protocol.validate_response(payload, "update_reminder"),
            payload,
        )

    def test_pending_move_fixture_satisfies_full_response_contract(self) -> None:
        payload = self.mutation_fixture(
            "move_reminder",
            "committed_verification_pending",
        )

        self.assertIs(
            eventkit_protocol.validate_response(payload, "move_reminder"),
            payload,
        )
        error = payload["error"]
        self.assertIsInstance(error, dict)
        self.assertEqual(error["code"], "sync_pending")

    def test_mutation_fixture_without_backend_is_rejected(self) -> None:
        payload = self.mutation_fixture("complete_reminder", "verified")
        del payload["backend"]

        with self.assertRaisesRegex(RuntimeError, "backend"):
            eventkit_protocol.validate_response(payload, "complete_reminder")


if __name__ == "__main__":
    unittest.main()
