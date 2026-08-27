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

from mcp.v2_core import V2CoreFacade
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
                        "truncated": False,
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
                    {
                        "reminder_id": REMINDER_ID,
                        "attachment_type": "url",
                        "limit": 200,
                    },
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

    def test_url_patch_replaces_single_attachment_matching_previous_metadata(self) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "33333333-3333-4333-8333-333333333333",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": old_url},
            "after": {"id": REMINDER_ID, "url": new_url},
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
                    "url": new_url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        bridge_call = mock.Mock(
            side_effect=[(eventkit_receipt, False), (final_read, False)]
        )
        adapter_call = mock.Mock(
            side_effect=[
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [
                            {"id": "ATTACHMENT-A", "type": "url", "url": old_url},
                            {
                                "id": "UNRELATED",
                                "type": "url",
                                "url": "https://example.com/unrelated",
                            },
                        ],
                        "truncated": False,
                    },
                    False,
                ),
                (
                    {
                        "ok": True,
                        "status": "verified",
                        "operation": "replace_attachment",
                        "operation_id": "44444444-4444-4444-8444-444444444444",
                        "backend": "sqlite_private",
                        "target": {
                            "reminder_id": REMINDER_ID,
                            "attachment_id": "ATTACHMENT-B",
                        },
                        "before": {},
                        "after": {
                            "attachment": {
                                "id": "ATTACHMENT-B",
                                "type": "url",
                                "url": new_url,
                            }
                        },
                        "verification": {"attachment_active": True},
                        "recovery": {"semantics": "replace_previous_attachment"},
                    },
                    False,
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
                "patch": {"url": new_url},
            },
            mutation=True,
        )

        self.assertEqual(reply.payload["status"], "verified")
        self.assertEqual(reply.payload["after"]["url_attachment"]["url"], new_url)
        replace_tool, replace_arguments = argv_calls[1]
        self.assertEqual(replace_tool, "replace_reminder_attachment")
        self.assertEqual(replace_arguments["attachment_id"], "ATTACHMENT-A")
        self.assertEqual(replace_arguments["url"], new_url)
        self.assertTrue(
            replace_arguments["idempotency_key"].startswith("core-url-replace-")
        )
        self.assertNotIn("UNRELATED", repr(replace_arguments))

    def test_url_patch_preserves_ambiguous_matching_attachments(self) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "55555555-5555-4555-8555-555555555555",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": old_url},
            "after": {"id": REMINDER_ID, "url": new_url},
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
                    "url": new_url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        bridge_call = mock.Mock(
            side_effect=[(eventkit_receipt, False), (final_read, False)]
        )
        adapter_call = mock.Mock(
            return_value=(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [
                        {"id": "A-1", "type": "url", "url": old_url},
                        {"id": "A-2", "type": "url", "url": old_url},
                    ],
                    "truncated": False,
                },
                False,
            )
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
                "patch": {"url": new_url},
            },
            mutation=True,
        )

        self.assertEqual(reply.payload["status"], "partial_success")
        self.assertEqual(
            reply.payload["error"]["reason_code"],
            "ambiguous_visible_url_attachment",
        )
        self.assertEqual(len(adapter_call.call_args_list), 1)
        self.assertEqual(
            [name for name, _ in argv_calls], ["list_reminder_attachments"]
        )
        self.assertTrue(reply.payload["verification"]["final_read"])

    def test_url_patch_reuses_one_existing_target_without_duplicate_write(self) -> None:
        url = "https://example.com/already-visible"
        existing = {"id": "ATTACHMENT-EXISTING", "type": "url", "url": url}
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "66666666-6666-4666-8666-666666666666",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": url},
            "after": {"id": REMINDER_ID, "url": url},
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
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
            side_effect=[(eventkit_receipt, False), (final_read, False)]
        )
        adapter_call = mock.Mock(
            return_value=(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [existing],
                    "truncated": False,
                },
                False,
            )
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        def build_argv(tool_name: str, arguments: dict[str, Any]) -> list[str]:
            argv_calls.append((tool_name, copy.deepcopy(arguments)))
            return [tool_name]

        reply = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=build_argv,
        ).invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": url},
            },
            mutation=True,
        )

        self.assertEqual(reply.payload["status"], "unchanged")
        self.assertEqual(reply.payload["after"]["url_attachment"], existing)
        self.assertFalse(
            reply.payload["verification"]["url_attachment"]["write_performed"]
        )
        self.assertEqual(
            [name for name, _ in argv_calls], ["list_reminder_attachments"]
        )
        self.assertEqual(adapter_call.call_count, 1)

    def test_unchanged_url_with_another_url_attachment_fails_closed_without_write(
        self,
    ) -> None:
        url = "https://example.com/already-visible"
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "77777777-7777-4777-8777-777777777777",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": url},
            "after": {"id": REMINDER_ID, "url": url},
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
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
            side_effect=[(eventkit_receipt, False), (final_read, False)]
        )
        adapter_call = mock.Mock(
            return_value=(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [
                        {"id": "ATTACHMENT-B", "type": "url", "url": url},
                        {
                            "id": "ATTACHMENT-C",
                            "type": "url",
                            "url": "https://example.com/unrelated",
                        },
                    ],
                    "truncated": False,
                },
                False,
            )
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        reply = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, arguments: (
                argv_calls.append((name, copy.deepcopy(arguments))) or [name]
            ),
        ).invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": url},
            },
            mutation=True,
        )

        self.assertTrue(reply.is_error)
        self.assertEqual(reply.payload["status"], "failed_no_mutation")
        self.assertEqual(reply.payload["error"]["code"], "ambiguous_scope")
        self.assertEqual(
            reply.payload["error"]["reason_code"],
            "ambiguous_visible_url_attachment",
        )
        self.assertFalse(reply.payload["verification"]["write_performed"])
        self.assertTrue(reply.payload["verification"]["final_read"])
        self.assertFalse(reply.payload["recovery"]["automatic_retry_safe"])
        self.assertEqual(reply.payload["next_action"]["tool"], "read_reminder")
        self.assertFalse(reply.payload["next_action"]["retry_original_once"])
        self.assertIn(
            "inspect_reminder_native",
            reply.payload["recovery"]["manual_action"],
        )
        self.assertIn(
            "change_reminder_attachment",
            reply.payload["recovery"]["manual_action"],
        )
        self.assertEqual(
            [name for name, _ in argv_calls], ["list_reminder_attachments"]
        )
        self.assertEqual(adapter_call.call_count, 1)

    def test_fresh_retry_after_partial_url_replace_does_not_hide_a_and_b(
        self,
    ) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"

        def reminder(url: str, last_modified: str) -> dict[str, Any]:
            return {
                "id": REMINDER_ID,
                "title": "URL retry regression",
                "url": url,
                "calendar_id": "LIST-1",
                "last_modified": last_modified,
            }

        def read_receipt(url: str, last_modified: str) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "read_reminder",
                "data": {"reminder": reminder(url, last_modified)},
            }

        first_eventkit_write = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "88888888-8888-4888-8888-888888888888",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": reminder(old_url, "2026-08-25T01:00:00.000Z"),
            "after": reminder(new_url, "2026-08-25T01:00:01.000Z"),
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
        retry_eventkit_no_write = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "99999999-9999-4999-8999-999999999999",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "after": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
        }
        bridge_call = mock.Mock(
            side_effect=[
                (read_receipt(old_url, "2026-08-25T01:00:00.000Z"), False),
                (first_eventkit_write, False),
                (read_receipt(new_url, "2026-08-25T01:00:01.000Z"), False),
                (read_receipt(new_url, "2026-08-25T01:00:01.000Z"), False),
                (retry_eventkit_no_write, False),
                (read_receipt(new_url, "2026-08-25T01:00:01.000Z"), False),
            ]
        )
        adapter_call = mock.Mock(
            side_effect=[
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [
                            {
                                "id": "ATTACHMENT-A",
                                "type": "url",
                                "url": old_url,
                            }
                        ],
                        "truncated": False,
                    },
                    False,
                ),
                (
                    {
                        "ok": False,
                        "status": "failed_manual_repair_required",
                        "operation": "replace_attachment",
                        "error": {
                            "code": "native_url_replace_uncertain",
                            "message": "The native replacement outcome is uncertain.",
                        },
                    },
                    True,
                ),
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 8,
                        "attachments": [
                            {
                                "id": "ATTACHMENT-A",
                                "type": "url",
                                "url": old_url,
                            },
                            {
                                "id": "ATTACHMENT-B",
                                "type": "url",
                                "url": new_url,
                            },
                        ],
                        "truncated": False,
                    },
                    False,
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []
        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, arguments: (
                argv_calls.append((name, copy.deepcopy(arguments))) or [name]
            ),
        )
        tokens = iter(["A" * 32, "B" * 32])
        facade = V2CoreFacade(backend, token_source=lambda: next(tokens))

        first_reference = facade.read_reminder({"reminder_id": REMINDER_ID})[
            "data"
        ]["reminder"]["reference"]
        first = facade.change_reminder(
            {
                "reference": first_reference,
                "action": {"kind": "patch", "patch": {"url": new_url}},
            }
        )
        fresh_reference = facade.read_reminder({"reminder_id": REMINDER_ID})[
            "data"
        ]["reminder"]["reference"]
        retry = facade.change_reminder(
            {
                "reference": fresh_reference,
                "action": {"kind": "patch", "patch": {"url": new_url}},
            }
        )

        self.assertEqual(first["status"], "partial_success")
        self.assertEqual(retry["status"], "failed_no_mutation")
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["error"]["code"], "ambiguous_scope")
        self.assertEqual(
            retry["error"]["reason_code"],
            "ambiguous_visible_url_attachment",
        )
        self.assertFalse(retry["verification"]["write_performed"])
        self.assertTrue(retry["verification"]["final_read"])
        self.assertIsNone(retry["before"])
        self.assertIsNone(retry["after"])
        self.assertNotIn("reference", repr(retry))
        self.assertIn(
            "inspect_reminder_native",
            retry["recovery"]["manual_action"],
        )
        self.assertIn(
            "change_reminder_attachment",
            retry["recovery"]["manual_action"],
        )
        self.assertEqual(
            [name for name, _ in argv_calls],
            [
                "list_reminder_attachments",
                "replace_reminder_attachment",
                "list_reminder_attachments",
            ],
        )

    def test_fresh_retry_with_only_stale_a_performs_no_native_write(self) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"

        def reminder(url: str, last_modified: str) -> dict[str, Any]:
            return {
                "id": REMINDER_ID,
                "title": "URL A-only retry regression",
                "url": url,
                "calendar_id": "LIST-1",
                "last_modified": last_modified,
            }

        def read_receipt(url: str, last_modified: str) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "read_reminder",
                "data": {"reminder": reminder(url, last_modified)},
            }

        first_eventkit_write = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "88888888-8888-4888-8888-888888888888",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": reminder(old_url, "2026-08-25T01:00:00.000Z"),
            "after": reminder(new_url, "2026-08-25T01:00:01.000Z"),
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
        retry_eventkit_no_write = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "99999999-9999-4999-8999-999999999999",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "after": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
        }
        bridge_call = mock.Mock(
            side_effect=[
                (read_receipt(old_url, "2026-08-25T01:00:00.000Z"), False),
                (first_eventkit_write, False),
                (read_receipt(new_url, "2026-08-25T01:00:01.000Z"), False),
                (read_receipt(new_url, "2026-08-25T01:00:01.000Z"), False),
                (retry_eventkit_no_write, False),
                (read_receipt(new_url, "2026-08-25T01:00:01.000Z"), False),
            ]
        )
        stale_a_inventory = {
            "ok": True,
            "reminder_id": REMINDER_ID,
            "reminder_version": 7,
            "attachments": [
                {"id": "ATTACHMENT-A", "type": "url", "url": old_url}
            ],
            "truncated": False,
        }
        adapter_call = mock.Mock(
            side_effect=[
                (copy.deepcopy(stale_a_inventory), False),
                (
                    {
                        "ok": False,
                        "status": "failed_no_mutation",
                        "operation": "replace_attachment",
                        "error": {
                            "code": "native_url_replace_failed",
                            "message": "The native replacement did not commit.",
                        },
                    },
                    True,
                ),
                (copy.deepcopy(stale_a_inventory), False),
                (
                    {
                        "ok": True,
                        "status": "verified",
                        "operation": "attach_url",
                        "after": {
                            "attachment": {
                                "id": "ATTACHMENT-B",
                                "type": "url",
                                "url": new_url,
                            }
                        },
                        "verification": {"attachment_active": True},
                        "recovery": {
                            "semantics": "not_applicable",
                            "automatic_retry_safe": True,
                        },
                    },
                    False,
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []
        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, arguments: (
                argv_calls.append((name, copy.deepcopy(arguments))) or [name]
            ),
        )
        tokens = iter(["C" * 32, "D" * 32])
        facade = V2CoreFacade(backend, token_source=lambda: next(tokens))

        first_reference = facade.read_reminder({"reminder_id": REMINDER_ID})[
            "data"
        ]["reminder"]["reference"]
        first = facade.change_reminder(
            {
                "reference": first_reference,
                "action": {"kind": "patch", "patch": {"url": new_url}},
            }
        )
        retry_reference = facade.read_reminder({"reminder_id": REMINDER_ID})[
            "data"
        ]["reminder"]["reference"]
        retry = facade.change_reminder(
            {
                "reference": retry_reference,
                "action": {"kind": "patch", "patch": {"url": new_url}},
            }
        )

        self.assertEqual(first["status"], "partial_success")
        self.assertEqual(retry["status"], "failed_no_mutation")
        self.assertEqual(retry["error"]["code"], "ambiguous_scope")
        self.assertEqual(
            retry["error"]["reason_code"],
            "ambiguous_visible_url_attachment",
        )
        self.assertFalse(retry["verification"]["write_performed"])
        self.assertEqual(
            [name for name, _ in argv_calls],
            [
                "list_reminder_attachments",
                "replace_reminder_attachment",
                "list_reminder_attachments",
            ],
        )
        self.assertEqual(adapter_call.call_count, 3)

    def test_url_retry_preserves_existing_a_and_b_instead_of_duplicating_b(self) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"
        payload = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "77777777-7777-4777-8777-777777777777",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": old_url},
            "after": {"id": REMINDER_ID, "url": new_url},
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
                    "url": new_url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        adapter_call = mock.Mock(
            return_value=(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [
                        {"id": "ATTACHMENT-A", "type": "url", "url": old_url},
                        {"id": "ATTACHMENT-B", "type": "url", "url": new_url},
                    ],
                    "truncated": False,
                },
                False,
            )
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        result = make_backend(
            bridge_call=mock.Mock(return_value=(final_read, False)),
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, arguments: (
                argv_calls.append((name, copy.deepcopy(arguments))) or [name]
            ),
        )._ensure_visible_url_attachment(copy.deepcopy(payload), new_url)

        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(
            result["error"]["reason_code"],
            "target_url_attachment_already_exists",
        )
        self.assertEqual(
            [name for name, _ in argv_calls], ["list_reminder_attachments"]
        )
        self.assertEqual(adapter_call.call_count, 1)

    def test_malformed_url_inventory_never_dispatches_an_attachment_write(self) -> None:
        url = "https://example.com/already-visible"
        base_payload = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "77777777-7777-4777-8777-777777777777",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": url},
            "after": {"id": REMINDER_ID, "url": url},
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
        }
        malformed_inventories = (
            {"ok": True, "reminder_id": REMINDER_ID, "reminder_version": 7},
            {
                "ok": True,
                "reminder_id": "WRONG-REMINDER",
                "reminder_version": 7,
                "attachments": [],
                "truncated": False,
            },
            {
                "ok": True,
                "reminder_id": REMINDER_ID,
                "reminder_version": 7,
                "attachments": [{"id": "URL-1", "type": "url"}],
                "truncated": False,
            },
            {
                "ok": True,
                "reminder_id": REMINDER_ID,
                "reminder_version": 7,
                "attachments": [],
            },
        )

        for inventory in malformed_inventories:
            with self.subTest(inventory=inventory):
                adapter_call = mock.Mock(return_value=(inventory, False))
                argv_calls: list[tuple[str, dict[str, Any]]] = []
                result = make_backend(
                    bridge_call=mock.Mock(),
                    adapter_call=adapter_call,
                    build_adapter_argv=lambda name, arguments: (
                        argv_calls.append((name, copy.deepcopy(arguments))) or [name]
                    ),
                )._ensure_visible_url_attachment(copy.deepcopy(base_payload), url)

                self.assertEqual(result["status"], "failed_no_mutation")
                self.assertEqual(result["error"]["code"], "ambiguous_scope")
                self.assertEqual(
                    result["error"]["reason_code"],
                    "native_url_attachment_inventory_invalid",
                )
                self.assertEqual(
                    [name for name, _ in argv_calls],
                    ["list_reminder_attachments"],
                )
                self.assertEqual(adapter_call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
