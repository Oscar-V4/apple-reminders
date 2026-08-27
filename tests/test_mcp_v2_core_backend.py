from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp.v2_core_backend import CoreBackend


REMINDER_ID = "REMINDER-EXACT-1"


class BridgeContract:
    def validate_mutation_receipt(
        self,
        payload: dict[str, Any],
        operation: str,
    ) -> None:
        if payload.get("operation") != operation:
            raise RuntimeError("operation mismatch")


class AdapterModule:
    AdapterError = ()

    @staticmethod
    def execute_idempotent(**arguments: Any) -> dict[str, Any]:
        return arguments["callback"]()


def make_backend(
    *,
    bridge_call: Any,
    adapter_call: Any | None = None,
    build_adapter_argv: Any | None = None,
    receipt_validator: Any | None = None,
) -> CoreBackend:
    return CoreBackend(
        bridge_call=bridge_call,
        adapter_call=adapter_call or mock.Mock(),
        build_adapter_argv=build_adapter_argv or mock.Mock(),
        adapter_module=lambda: AdapterModule,
        bridge_module=lambda: BridgeContract(),
        receipt_validator=receipt_validator or mock.Mock(return_value=None),
    )


class CoreBackendInterfaceTests(unittest.TestCase):
    def test_maps_reads_and_mutations_without_rewriting_arguments(self) -> None:
        read_payload = {
            "schema_version": 1,
            "operation": "fetch_reminders",
            "status": "verified",
            "ok": True,
            "data": {"items": []},
        }
        mutation_payload = {
            "schema_version": 1,
            "operation": "update_reminder",
            "status": "verified",
            "ok": True,
        }
        bridge_call = mock.Mock(
            side_effect=[(read_payload, False), (mutation_payload, False)]
        )
        backend = make_backend(bridge_call=bridge_call)
        read_arguments = {
            "calendar_ids": ["LIST-1"],
            "limit": 10,
            "offset": 37,
        }
        mutation_arguments = {
            "reminder_id": REMINDER_ID,
            "expected_last_modified": "2026-08-25T00:00:00Z",
            "patch": {"title": "Changed"},
        }

        read_reply = backend.invoke(
            "fetch_reminders",
            read_arguments,
            mutation=False,
        )
        mutation_reply = backend.invoke(
            "update_reminder",
            mutation_arguments,
            mutation=True,
        )

        self.assertEqual(read_reply.payload, read_payload)
        self.assertFalse(read_reply.is_error)
        self.assertEqual(mutation_reply.payload, mutation_payload)
        self.assertFalse(mutation_reply.is_error)
        self.assertEqual(
            bridge_call.call_args_list,
            [
                mock.call("fetch_reminders", read_arguments),
                mock.call("update_reminder", mutation_arguments),
            ],
        )

    def test_hybrid_url_failure_preserves_partial_receipt_and_final_read_order(
        self,
    ) -> None:
        url = "https://example.com/spec"
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": None,
            "after": {
                "id": REMINDER_ID,
                "url": url,
                "calendar_id": "LIST-1",
                "last_modified": "2026-08-25T01:00:00.000Z",
            },
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "eventkit_native_api",
                "automatic_retry_safe": False,
            },
        }
        final_read = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        bridge_call = mock.Mock(
            side_effect=[(copy.deepcopy(eventkit_receipt), False), (final_read, False)]
        )
        adapter_call = mock.Mock(
            side_effect=[
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                    },
                    False,
                ),
                (
                    {
                        "ok": False,
                        "status": "failed_no_mutation",
                        "error": {
                            "code": "native_url_attachment_failed",
                            "message": "The native URL attachment was not saved.",
                        },
                    },
                    True,
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        def build_argv(tool_name: str, arguments: dict[str, Any]) -> list[str]:
            argv_calls.append((tool_name, copy.deepcopy(arguments)))
            return [tool_name]

        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=build_argv,
        )

        reply = backend.invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": url},
            },
            mutation=True,
        )

        self.assertFalse(reply.is_error)
        self.assertEqual(reply.payload["status"], "partial_success")
        self.assertEqual(
            reply.payload["error"]["reason_code"],
            "native_url_attachment_failed",
        )
        self.assertFalse(reply.payload["recovery"]["automatic_retry_safe"])
        self.assertEqual(reply.payload["verification"]["state"], "partial")
        self.assertTrue(reply.payload["verification"]["final_read"])
        self.assertEqual(
            [call.args[0] for call in bridge_call.call_args_list],
            ["update_reminder", "read_reminder"],
        )
        self.assertEqual(
            argv_calls,
            [
                (
                    "list_reminder_attachments",
                    {"reminder_id": REMINDER_ID, "limit": 1},
                ),
                (
                    "attach_url_to_reminder",
                    {
                        "reminder_id": REMINDER_ID,
                        "url": url,
                        "if_version": 7,
                    },
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
