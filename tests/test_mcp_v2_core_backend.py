from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import durable_idempotency
from durable_idempotency import execute_idempotent
from mcp import server as mcp_server
from mcp.v2_core import V2CoreFacade
from mcp.v2_core_backend import CoreBackend
from mcp.v2_transport import DispatchCertainty, TransportResult


REMINDER_ID = "REMINDER-EXACT-1"


def transport(
    payload: dict[str, Any],
    *,
    is_error: bool | None = None,
    proves_not_started: bool = False,
) -> TransportResult:
    """Build the typed result returned by the local bridge/adapter launchers."""
    return TransportResult(
        payload=payload,
        is_error=payload.get("ok") is not True if is_error is None else is_error,
        dispatch_certainty=(
            DispatchCertainty.PROVEN_NOT_STARTED
            if proves_not_started
            else DispatchCertainty.MAY_HAVE_STARTED
        ),
    )


def valid_eventkit_receipt(
    operation: str,
    *,
    status: str = "verified",
    **overrides: Any,
) -> dict[str, Any]:
    """Build a complete EventKit mutation Receipt for Core transport tests."""
    write_performed = status == "verified"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "ok": True,
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "backend": "eventkit_public_sdk",
        "target": {"id": REMINDER_ID},
        "after": {"id": REMINDER_ID},
        "verification": {
            "state": "read_back",
            "write_performed": write_performed,
            "final_read": True,
            "matched": True,
        },
        "recovery": {
            "semantics": "not_applicable",
            "automatic_retry_safe": not write_performed,
        },
    }
    if operation != "create_reminder":
        payload["before"] = {"id": REMINDER_ID}
    payload.update(copy.deepcopy(overrides))
    return payload


def idempotency_passthrough(**arguments: Any) -> dict[str, Any]:
    return arguments["callback"]()


def bound_idempotency(storage_dir: Path) -> Any:
    return partial(execute_idempotent, storage_dir=storage_dir)


def make_backend(
    *,
    bridge_call: Any,
    adapter_call: Any | None = None,
    build_adapter_argv: Any | None = None,
    receipt_validator: Any | None = None,
    idempotency_call: Any | None = None,
) -> CoreBackend:
    return CoreBackend(
        bridge_call=bridge_call,
        adapter_call=adapter_call or mock.Mock(),
        build_adapter_argv=build_adapter_argv or mock.Mock(),
        idempotency_call=idempotency_call or idempotency_passthrough,
        receipt_validator=receipt_validator or mock.Mock(return_value=None),
    )


class CoreBackendInterfaceTests(unittest.TestCase):
    def test_server_composes_core_without_in_process_backend_loaders(self) -> None:
        self.assertFalse(hasattr(mcp_server, "_ADAPTER_MODULE"))
        self.assertFalse(hasattr(mcp_server, "bundled_adapter_module"))
        self.assertFalse(hasattr(mcp_server, "_EVENTKIT_BRIDGE_MODULE"))
        self.assertFalse(hasattr(mcp_server, "bundled_eventkit_bridge_module"))
        self.assertFalse(hasattr(mcp_server, "_load_local_module"))
        dispatch = mcp_server._LocalToolDispatch(mcp_server.DEFAULT_BACKEND_PATHS)

        with (
            mock.patch("mcp.v2_core_backend.CoreBackend") as backend_type,
            mock.patch("mcp.v2_core.V2CoreFacade") as facade_type,
        ):
            facade = dispatch.core_facade()

        self.assertIs(facade, facade_type.return_value)
        kwargs = backend_type.call_args.kwargs
        self.assertIs(kwargs["idempotency_call"], mcp_server.execute_idempotent)
        self.assertNotIn("adapter_module", kwargs)
        self.assertNotIn("bridge_module", kwargs)

    def test_create_uses_narrow_idempotency_without_importing_adapter(self) -> None:
        def loaded_adapter_modules() -> set[str]:
            return {
                name
                for name, module in sys.modules.items()
                if Path(str(getattr(module, "__file__", ""))).name
                == "reminders_adapter.py"
            }

        payload = valid_eventkit_receipt("create_reminder")
        idempotency_call = mock.Mock(side_effect=idempotency_passthrough)
        backend = make_backend(
            bridge_call=mock.Mock(return_value=transport(payload)),
            idempotency_call=idempotency_call,
        )
        before = loaded_adapter_modules()

        reply = backend.invoke(
            "create_reminder",
            {
                "calendar_id": "LIST-1",
                "title": "Narrow dependency",
                "idempotency_key": "narrow-idempotency",
            },
            mutation=True,
        )

        self.assertFalse(reply.is_error)
        self.assertEqual(loaded_adapter_modules(), before)
        idempotency_call.assert_called_once()

    def test_non_create_mutation_does_not_call_idempotency(self) -> None:
        payload = valid_eventkit_receipt("update_reminder")
        idempotency_call = mock.Mock(
            side_effect=RuntimeError("idempotency unavailable")
        )
        backend = CoreBackend(
            bridge_call=mock.Mock(return_value=transport(payload)),
            adapter_call=mock.Mock(),
            build_adapter_argv=mock.Mock(),
            idempotency_call=idempotency_call,
            receipt_validator=mock.Mock(return_value=None),
        )

        reply = backend.invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T00:00:00Z",
                "patch": {"title": "Changed"},
            },
            mutation=True,
        )

        self.assertFalse(reply.is_error)
        self.assertEqual(reply.payload, payload)
        idempotency_call.assert_not_called()

    def test_create_proven_no_write_bridge_failure_clears_fence_for_retry(
        self,
    ) -> None:
        failed = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "permission_denied",
                "reason_code": "reminders_access_denied",
                "message": "Full Reminders access is required",
                "category": "permission_denied",
                "retryable": False,
                "details": {},
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(failed)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Safe retry",
            "idempotency_key": "create-no-write-retry",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            store = support / "idempotency.json"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke(
                "create_reminder",
                arguments,
                mutation=True,
            )
            second = backend.invoke(
                "create_reminder",
                arguments,
                mutation=True,
            )
            stored = json.loads(store.read_text(encoding="utf-8"))

        self.assertTrue(first.is_error)
        self.assertTrue(second.is_error)
        self.assertEqual(first.payload, failed)
        self.assertEqual(second.payload, failed)
        self.assertEqual(bridge_call.call_count, 2)
        self.assertEqual(stored["entries"], {})

    def test_create_child_spoofed_no_write_flags_remain_fenced(self) -> None:
        failed = {
            "ok": False,
            "__dispatch_phase": "not_started",
            "mutation_not_started": True,
            "error": {
                "code": "eventkit_bridge_unavailable",
                "message": "The bundled EventKit bridge is unavailable.",
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(failed)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Unclassified failure",
            "idempotency_key": "create-unknown-failure",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke(
                "create_reminder",
                arguments,
                mutation=True,
            )
            replay = backend.invoke(
                "create_reminder",
                arguments,
                mutation=True,
            )

        self.assertTrue(first.is_error)
        self.assertEqual(first.payload, failed)
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertEqual(replay.mutation_state, "unknown")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_parent_proven_prelaunch_failure_clears_fence(self) -> None:
        not_started = {
            "ok": False,
            "error": {
                "code": "eventkit_bridge_unavailable",
                "message": "The bundled EventKit bridge is unavailable.",
            },
        }
        bridge_call = mock.Mock(
            return_value=transport(
                copy.deepcopy(not_started),
                proves_not_started=True,
            )
        )
        arguments = {
            "list_id": "LIST-1",
            "title": "Retry prelaunch",
            "idempotency_key": "create-parent-prelaunch",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            store = support / "idempotency.json"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            facade = V2CoreFacade(backend)
            first, first_state = facade.call_with_state(
                "create_reminder", arguments
            )
            second, second_state = facade.call_with_state(
                "create_reminder", arguments
            )
            stored = json.loads(store.read_text(encoding="utf-8"))

        for receipt in (first, second):
            self.assertFalse(receipt["ok"])
            self.assertEqual(receipt["status"], "failed_no_mutation")
            self.assertEqual(receipt["operation"], "create_reminder")
            self.assertFalse(receipt["verification"]["write_performed"])
            self.assertNotIn("__dispatch_phase", repr(receipt))
        self.assertEqual(bridge_call.call_count, 2)
        self.assertEqual(stored["entries"], {})
        self.assertEqual(first_state, "not_mutated")
        self.assertEqual(second_state, "not_mutated")

    def test_create_pending_bridge_receipt_replays_without_redispatch(self) -> None:
        pending = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "committed_verification_pending",
            "ok": True,
            "operation_id": "11111111-1111-4111-8111-111111111111",
            "backend": "eventkit_public_sdk",
            "target": {},
            "after": {},
            "verification": {"state": "pending", "write_performed": None},
            "recovery": {
                "semantics": "read_before_retry",
                "automatic_retry_safe": False,
            },
            "warnings": [
                {
                    "code": "verification_pending",
                    "message": "Read before retrying.",
                }
            ],
            "error": {
                "code": "sync_pending",
                "reason_code": "native_timeout",
                "message": "The EventKit outcome is unknown.",
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(pending)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Pending create",
            "idempotency_key": "create-pending",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertFalse(first.is_error)
        self.assertEqual(first.payload["status"], "committed_verification_pending")
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(first.payload.get("replayed", False))
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertEqual(replay.mutation_state, "unknown")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_committed_projection_replay_preserves_state(self) -> None:
        verified = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "create_reminder",
            "operation_id": "12345678-1234-4234-9234-1234567890ab",
            "backend": "eventkit_public_sdk",
            "target": {"calendar_id": "LIST-1"},
            "after": {"calendar_id": "LIST-1"},
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": False,
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(verified)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Committed replay",
            "url": "https://example.com/item",
            "idempotency_key": "create-committed-replay",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertEqual(first.payload["status"], "partial_success")
        self.assertEqual(first.mutation_state, "committed")
        self.assertEqual(replay.payload["status"], "partial_success")
        self.assertEqual(replay.mutation_state, "committed")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_replay_stays_committed_after_private_recurrence_redaction(
        self,
    ) -> None:
        verified = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "create_reminder",
            "operation_id": "99999999-9999-4999-8999-999999999999",
            "backend": "eventkit_public_sdk",
            "target": {"reminder_id": "R-RECURRENCE"},
            "after": {
                "reminder_id": "R-RECURRENCE",
                "recurrence_rules": [
                    {
                        "frequency": "monthly",
                        "days_of_month": [5, 20],
                        "end": {"count": 12},
                    }
                ],
            },
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
                "target_fields": ["title", "recurrence_rules"],
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": False,
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(verified)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Private recurring schedule",
            "idempotency_key": "create-private-recurrence",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(Path(temp_dir) / "support"),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertEqual(first.mutation_state, "committed")
        self.assertIn("recurrence_rules", first.payload["after"])
        self.assertEqual(replay.mutation_state, "committed")
        self.assertEqual(replay.payload["after"], {"reminder_id": "R-RECURRENCE"})
        self.assertNotIn("target_fields", replay.payload["verification"])
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_invalid_success_receipt_remains_fenced(self) -> None:
        invalid = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "verified",
            "ok": True,
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(invalid)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Invalid receipt",
            "idempotency_key": "create-invalid-success",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertTrue(first.is_error)
        self.assertEqual(first.payload["error"]["code"], "invalid_eventkit_receipt")
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_validator_rejected_no_write_label_remains_fenced(self) -> None:
        rejected = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "untrusted_error_code",
                "message": "This label did not cross the bridge contract.",
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(rejected)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Rejected label",
            "idempotency_key": "create-rejected-label",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertTrue(first.is_error)
        self.assertEqual(first.payload, rejected)
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_contradictory_no_write_evidence_remains_fenced(self) -> None:
        contradictory = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "after": {"id": REMINDER_ID},
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
            },
            "error": {
                "code": "permission_denied",
                "reason_code": "reminders_access_denied",
                "message": "Contradictory write evidence",
                "retryable": False,
            },
        }
        bridge_call = mock.Mock(
            return_value=transport(copy.deepcopy(contradictory))
        )
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Contradictory no-write",
            "idempotency_key": "create-contradictory-no-write",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertTrue(first.is_error)
        self.assertEqual(first.payload, contradictory)
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_no_write_cleanup_failure_keeps_fence_fail_closed(self) -> None:
        failed = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "permission_denied",
                "reason_code": "reminders_access_denied",
                "message": "Full Reminders access is required",
                "category": "permission_denied",
                "retryable": False,
                "details": {},
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(failed)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Cleanup failure",
            "idempotency_key": "create-cleanup-failure",
        }
        writes = 0
        real_write = durable_idempotency._write_store

        def fail_cleanup(
            payload: dict[str, Any],
            *,
            storage_dir: Path,
            store_path: Path,
        ) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("disk full")
            real_write(
                payload,
                storage_dir=storage_dir,
                store_path=store_path,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            with mock.patch.object(
                durable_idempotency,
                "_write_store",
                side_effect=fail_cleanup,
            ):
                first = backend.invoke("create_reminder", arguments, mutation=True)
                replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertTrue(first.is_error)
        self.assertEqual(first.payload["status"], "failed_no_mutation")
        self.assertEqual(
            first.payload["error"]["details"]["reason_code"],
            "idempotency_fence_cleanup_failed",
        )
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_maps_reads_and_mutations_without_rewriting_arguments(self) -> None:
        read_payload = {
            "schema_version": 1,
            "operation": "fetch_reminders",
            "status": "verified",
            "ok": True,
            "data": {"items": []},
        }
        mutation_payload = valid_eventkit_receipt("update_reminder")
        bridge_call = mock.Mock(
            side_effect=[transport(read_payload), transport(mutation_payload)]
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
        self.assertIsNone(read_reply.mutation_state)
        self.assertEqual(mutation_reply.payload, mutation_payload)
        self.assertFalse(mutation_reply.is_error)
        self.assertEqual(mutation_reply.mutation_state, "committed")
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
            "before": {},
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
            side_effect=[
                transport(copy.deepcopy(eventkit_receipt)),
                transport(final_read),
            ]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": False,
                        "status": "failed_no_mutation",
                        "error": {
                            "code": "native_url_attachment_failed",
                            "message": "The native URL attachment was not saved.",
                        },
                    },
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
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
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
                    }
                ),
                transport(
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
                    }
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
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            return_value=transport(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [
                        {"id": "A-1", "type": "url", "url": old_url},
                        {"id": "A-2", "type": "url", "url": old_url},
                    ],
                    "truncated": False,
                }
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
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            return_value=transport(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [existing],
                    "truncated": False,
                }
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

    def test_native_url_write_promotes_composite_unchanged_to_committed(self) -> None:
        url = "https://example.com/new-visible-url"
        eventkit_receipt = valid_eventkit_receipt(
            "update_reminder",
            status="unchanged",
            target={"id": REMINDER_ID},
            before={"id": REMINDER_ID, "url": url},
            after={"id": REMINDER_ID, "url": url},
        )
        final_read = {
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": url,
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        attachment = {
            "id": "ATTACHMENT-NEW",
            "type": "url",
            "url": url,
        }
        bridge_call = mock.Mock(
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "status": "verified",
                        "operation": "attach_url",
                        "after": {"attachment": attachment},
                        "verification": {"attachment_active": True},
                        "recovery": {"semantics": "not_applicable"},
                    }
                ),
            ]
        )
        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, _arguments: [name],
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

        self.assertEqual(reply.payload["status"], "verified")
        self.assertTrue(reply.payload["verification"]["write_performed"])
        self.assertEqual(reply.payload["after"]["url_attachment"], attachment)
        self.assertEqual(reply.mutation_state, "committed")

    def test_native_url_commit_survives_failed_composite_final_read(self) -> None:
        url = "https://example.com/new-visible-url"
        eventkit_receipt = valid_eventkit_receipt(
            "update_reminder",
            status="unchanged",
            target={"id": REMINDER_ID},
            before={"id": REMINDER_ID, "url": url},
            after={"id": REMINDER_ID, "url": url},
        )
        bridge_call = mock.Mock(
            side_effect=[
                transport(eventkit_receipt),
                transport(
                    {
                        "ok": False,
                        "error": {
                            "code": "sync_pending",
                            "reason_code": "final_read_lost",
                        },
                    },
                    is_error=True,
                ),
            ]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "status": "verified",
                        "operation": "attach_url",
                        "after": {
                            "attachment": {
                                "id": "ATTACHMENT-NEW",
                                "type": "url",
                                "url": url,
                            }
                        },
                        "verification": {"attachment_active": True},
                        "recovery": {"semantics": "not_applicable"},
                    }
                ),
            ]
        )
        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, _arguments: [name],
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

        self.assertEqual(reply.payload["status"], "committed_verification_pending")
        self.assertTrue(reply.payload["verification"]["write_performed"])
        self.assertFalse(reply.payload["verification"]["final_read"])
        self.assertEqual(reply.mutation_state, "committed")

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
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            return_value=transport(
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
                }
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
        self.assertFalse(reply.payload["verification"]["final_read"])
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
                transport(read_receipt(old_url, "2026-08-25T01:00:00.000Z")),
                transport(first_eventkit_write),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(retry_eventkit_no_write),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
            ]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
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
                    }
                ),
                transport(
                    {
                        "ok": False,
                        "status": "failed_manual_repair_required",
                        "operation": "replace_attachment",
                        "error": {
                            "code": "native_url_replace_uncertain",
                            "message": "The native replacement outcome is uncertain.",
                        },
                    }
                ),
                transport(
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
                    }
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
        self.assertFalse(retry["verification"]["final_read"])
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
                transport(read_receipt(old_url, "2026-08-25T01:00:00.000Z")),
                transport(first_eventkit_write),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(retry_eventkit_no_write),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
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
                transport(copy.deepcopy(stale_a_inventory)),
                transport(
                    {
                        "ok": False,
                        "status": "failed_no_mutation",
                        "operation": "replace_attachment",
                        "error": {
                            "code": "native_url_replace_failed",
                            "message": "The native replacement did not commit.",
                        },
                    }
                ),
                transport(copy.deepcopy(stale_a_inventory)),
                transport(
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
                    }
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
            return_value=transport(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [
                        {"id": "ATTACHMENT-A", "type": "url", "url": old_url},
                        {"id": "ATTACHMENT-B", "type": "url", "url": new_url},
                    ],
                    "truncated": False,
                }
            )
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        result = make_backend(
            bridge_call=mock.Mock(return_value=transport(final_read)),
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
                adapter_call = mock.Mock(return_value=transport(inventory))
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
