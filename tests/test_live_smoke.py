from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
import unittest
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_SMOKE_PATH = REPO_ROOT / "scripts" / "live_smoke.py"
SPEC = importlib.util.spec_from_file_location("live_smoke", LIVE_SMOKE_PATH)
assert SPEC and SPEC.loader
live_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = live_smoke
SPEC.loader.exec_module(live_smoke)


REF_1 = "rev1." + "A" * 32
REF_2 = "rev1." + "B" * 32
REF_3 = "rev1." + "C" * 32
REF_4 = "rev1." + "D" * 32
REF_5 = "rev1." + "E" * 32
REF_6 = "rev1." + "F" * 32
REF_7 = "rev1." + "G" * 32
REF_8 = "rev1." + "H" * 32
REF_9 = "rev1." + "I" * 32
REF_10 = "rev1." + "J" * 32
REF_11 = "rev1." + "K" * 32
REF_12 = "rev1." + "L" * 32
LIST_ID = "LIST-ID-SECRET"
SECTION_ID = "SECTION-ID-SECRET"
REMINDER_ID = "REMINDER-ID-SECRET"
OLD_IMAGE_ID = "ATTACHMENT-ID-SECRET"
NEW_IMAGE_ID = "REPLACEMENT-ID-SECRET"


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        expected_name, response = self.responses.pop(0)
        if name != expected_name:
            raise AssertionError(f"expected {expected_name}, got {name}")
        action = arguments.get("action", {})
        if name == "change_reminder_attachment" and action.get("kind") in {
            "attach_image",
            "replace_image",
        }:
            image = Path(arguments["action"]["image_path"])
            if not image.is_file() or not image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                raise AssertionError("the harness did not create a synthetic PNG")
        return response


def successful_responses(*, experimental=False):
    responses = [
        (
            "ensure_reminder_list",
            {
                "ok": True,
                "status": "verified",
                "target": {"list_id": LIST_ID},
                "after": {"id": LIST_ID},
                "replayed": False,
            },
        ),
        (
            "ensure_reminder_list",
            {
                "ok": True,
                "status": "verified",
                "target": {"list_id": LIST_ID},
                "after": {"id": LIST_ID},
                "replayed": True,
            },
        ),
        (
            "create_reminder_section",
            {
                "ok": True,
                "status": "verified",
                "target": {"section_id": SECTION_ID},
                "after": {"id": SECTION_ID},
            },
        ),
        (
            "create_reminder",
            {
                "ok": True,
                "status": "verified",
                "target": {"reminder_id": REMINDER_ID},
                "after": {"id": REMINDER_ID, "reference": "rev1." + "Z" * 32},
                "replayed": False,
            },
        ),
        (
            "create_reminder",
            {
                "ok": True,
                "status": "verified",
                "target": {"reminder_id": REMINDER_ID},
                "after": {"id": REMINDER_ID, "reference": "rev1." + "Y" * 32},
                "replayed": True,
            },
        ),
        (
            "fetch_reminders",
            {
                "ok": True,
                "status": "verified",
                "data": {
                    "items": [
                        {
                            "id": REMINDER_ID,
                            "list_id": LIST_ID,
                            "title": "Codex synthetic live smoke reminder",
                        }
                    ],
                    "limit": 5,
                    "returned": 1,
                    "has_more": False,
                },
            },
        ),
        (
            "read_reminder",
            {
                "ok": True,
                "status": "verified",
                "data": {
                    "reminder": {
                        "id": REMINDER_ID,
                        "list_id": LIST_ID,
                        "title": "Codex synthetic live smoke reminder",
                        "url": "https://example.com/apple-reminders-live-smoke",
                        "reference": REF_1,
                    }
                },
            },
        ),
        (
            "read_reminder",
            {
                "ok": True,
                "status": "verified",
                "data": {
                    "reminder": {
                        "id": REMINDER_ID,
                        "list_id": LIST_ID,
                        "title": "Codex synthetic live smoke reminder",
                        "url": "https://example.com/apple-reminders-live-smoke",
                        "reference": REF_2,
                    }
                },
            },
        ),
        (
            "change_reminder",
            {"ok": True, "status": "verified", "after": {"reference": REF_3}},
        ),
        (
            "change_reminder",
            {
                "ok": False,
                "status": "failed_no_mutation",
                "error": {
                    "code": "concurrent_modification",
                    "reason_code": "concurrent_modification",
                },
            },
        ),
        (
            "change_reminder",
            {"ok": True, "status": "verified", "after": {"reference": REF_4}},
        ),
        (
            "change_reminder",
            {"ok": True, "status": "verified", "after": {"reference": REF_5}},
        ),
        (
            "organize_reminder",
            {
                "ok": True,
                "status": "verified",
                "after": {
                    "reference": REF_6,
                    "section": {"id": SECTION_ID},
                },
            },
        ),
        (
            "change_reminder_attachment",
            {
                "ok": True,
                "status": "verified",
                "after": {
                    "reference": REF_7,
                    "attachments": [
                        {
                            "id": "URL-ATTACHMENT-ID-SECRET",
                            "type": "url",
                            "url": "https://example.com/apple-reminders-live-smoke",
                        },
                        {"id": OLD_IMAGE_ID, "type": "image"},
                    ],
                },
            },
        ),
        (
            "inspect_reminder_native",
            {
                "ok": True,
                "status": "verified",
                "data": {
                    "reference": REF_8,
                    "section": {"id": SECTION_ID},
                    "attachments": [
                        {
                            "id": "URL-ATTACHMENT-ID-SECRET",
                            "type": "url",
                            "url": "https://example.com/apple-reminders-live-smoke",
                            "sync": {"fields_available": False},
                        },
                        {
                            "id": OLD_IMAGE_ID,
                            "type": "image",
                            "sync": {"mobile_visible_likely": True},
                        },
                    ],
                    "sync": {"fields_available": False},
                    "truncated": False,
                },
            },
        ),
        (
            "change_reminder_attachment",
            {
                "ok": True,
                "status": "verified",
                "after": {
                    "reference": REF_9,
                    "attachments": [
                        {
                            "id": "URL-ATTACHMENT-ID-SECRET",
                            "type": "url",
                            "url": "https://example.com/apple-reminders-live-smoke",
                        },
                        {"id": NEW_IMAGE_ID, "type": "image"},
                    ],
                },
            },
        ),
        (
            "inspect_reminder_native",
            {
                "ok": True,
                "status": "verified",
                "data": {
                    "reference": REF_10,
                    "section": {"id": SECTION_ID},
                    "attachments": [
                        {
                            "id": "URL-ATTACHMENT-ID-SECRET",
                            "type": "url",
                            "url": "https://example.com/apple-reminders-live-smoke",
                            "sync": {"fields_available": False},
                        },
                        {
                            "id": NEW_IMAGE_ID,
                            "type": "image",
                            "sync": {"mobile_visible_likely": True},
                        },
                    ],
                    "sync": {"fields_available": False},
                    "truncated": False,
                },
            },
        ),
        (
            "change_reminder_attachment",
            {
                "ok": True,
                "status": "verified",
                "after": {
                    "reference": REF_11,
                    "attachments": [
                        {
                            "id": "URL-ATTACHMENT-ID-SECRET",
                            "type": "url",
                            "url": "https://example.com/apple-reminders-live-smoke",
                        }
                    ],
                },
            },
        ),
        (
            "inspect_reminder_native",
            {
                "ok": True,
                "status": "verified",
                "data": {
                    "reference": REF_12,
                    "section": {"id": SECTION_ID},
                    "attachments": [
                        {
                            "id": "URL-ATTACHMENT-ID-SECRET",
                            "type": "url",
                            "url": "https://example.com/apple-reminders-live-smoke",
                            "sync": {"fields_available": False},
                        }
                    ],
                    "sync": {"fields_available": False},
                    "truncated": False,
                },
            },
        ),
        (
            "delete_reminder",
            {
                "ok": True,
                "status": "verified",
                "after": {"reminder_id": REMINDER_ID, "deleted": True},
                "verification": {"local_absence": True},
            },
        ),
        (
            "read_reminder",
            {
                "ok": False,
                "status": "failed_no_mutation",
                "error": {"code": "not_found", "reason_code": "reminder_not_found"},
            },
        ),
    ]

    if experimental:
        return responses
    experimental_tools = {
        "create_reminder_section",
        "organize_reminder",
        "change_reminder_attachment",
        "inspect_reminder_native",
    }
    core = [
        (name, payload) for name, payload in responses if name not in experimental_tools
    ]
    for name, payload in core:
        if name == "read_reminder" and payload.get("ok") is True:
            reminder = payload["data"]["reminder"]
            reminder["notes"] = (
                "Synthetic public MCP live smoke data.\n"
                "https://example.com/apple-reminders-live-smoke"
            )
    return core


class LiveSmokeCliGateTests(unittest.TestCase):
    def test_confirmation_and_source_identity_are_both_required_before_launch(self) -> None:
        client_factory = mock.Mock(side_effect=AssertionError("must not launch"))
        cleanup = mock.Mock(side_effect=AssertionError("must not clean"))

        for arguments in (
            [],
            ["--source-id", "SOURCE-SECRET"],
            ["--confirm-live-reminders"],
        ):
            with self.subTest(arguments=arguments):
                output = __import__("io").StringIO()
                self.assertEqual(
                    live_smoke.main(
                        arguments,
                        client_factory=client_factory,
                        cleanup=cleanup,
                        stdout=output,
                    ),
                    2,
                )
                self.assertRegex(
                    output.getvalue(),
                    r"^step=preflight status=blocked latency_ms=0\.000\n$",
                )

        client_factory.assert_not_called()
        cleanup.assert_not_called()

    def test_confirmed_cli_targets_the_installable_runtime_and_redacts_source(self) -> None:
        client = SequenceClient(successful_responses())
        cleanup = mock.Mock(return_value=True)
        captured = {}

        @contextmanager
        def client_factory(**arguments):
            captured.update(arguments)
            yield client

        output = io.StringIO()
        with mock.patch.object(
            live_smoke,
            "_synthetic_list_name",
            return_value="Codex-Apple-Reminders-Live-Smoke-cligate",
        ):
            result = live_smoke.main(
                ["--confirm-live-reminders", "--source-id", "SOURCE-ID-SECRET"],
                client_factory=client_factory,
                cleanup=cleanup,
                stdout=output,
            )

        self.assertEqual(result, 0)
        self.assertEqual(captured["server_path"], live_smoke.SERVER_PATH)
        self.assertEqual(captured["plugin_root"], live_smoke.PLUGIN_ROOT)
        self.assertIs(captured["experimental"], False)
        self.assertNotIn("SOURCE-ID-SECRET", output.getvalue())

    def test_experimental_cli_opt_in_reaches_server_and_workflow(self) -> None:
        client = SequenceClient(successful_responses(experimental=True))
        captured = {}

        @contextmanager
        def client_factory(**arguments):
            captured.update(arguments)
            yield client

        result = live_smoke.main(
            [
                "--confirm-live-reminders", "--source-id", "SOURCE-ID-SECRET",
                "--experimental",
            ],
            client_factory=client_factory,
            cleanup=mock.Mock(return_value=True),
            stdout=io.StringIO(),
        )

        self.assertEqual(result, 0)
        self.assertIs(captured["experimental"], True)
        self.assertEqual(client.responses, [])
        self.assertIn("create_reminder_section", [name for name, _ in client.calls])

    def test_stdio_server_enables_experimental_only_with_explicit_option(self) -> None:
        for experimental in (False, True):
            with self.subTest(experimental=experimental):
                process = mock.Mock()
                with (
                    mock.patch.object(
                        live_smoke.subprocess, "Popen", return_value=process
                    ) as launch,
                    mock.patch.object(
                        live_smoke.McpStdioClient, "_request",
                        return_value={"serverInfo": {}},
                    ),
                    mock.patch.object(live_smoke.McpStdioClient, "_notify"),
                ):
                    with live_smoke.McpStdioClient(experimental=experimental):
                        pass
                expected = [sys.executable, str(live_smoke.SERVER_PATH.resolve())]
                if experimental:
                    expected.append("--experimental")
                self.assertEqual(launch.call_args.args[0], expected)


class CoreLiveSmokeWorkflowTests(unittest.TestCase):
    def test_default_workflow_exercises_core_and_keeps_link_in_notes(self) -> None:
        client = SequenceClient(successful_responses())
        cleanup = mock.Mock(return_value=True)
        output = io.StringIO()
        list_name = "Codex-Apple-Reminders-Live-Smoke-core"

        with mock.patch.object(
            live_smoke, "_write_tiny_png",
            side_effect=AssertionError("Core must not create attachment files"),
        ):
            live_smoke.run_public_mcp_smoke(
                client,
                source_id="SOURCE-ID-SECRET",
                cleanup=cleanup,
                stdout=output,
                list_name_factory=lambda: list_name,
            )

        self.assertEqual(client.responses, [])
        self.assertEqual(
            [name for name, _ in client.calls],
            [
                "ensure_reminder_list", "ensure_reminder_list",
                "create_reminder", "create_reminder", "fetch_reminders",
                "read_reminder", "read_reminder", "change_reminder",
                "change_reminder", "change_reminder", "change_reminder",
                "delete_reminder", "read_reminder",
            ],
        )
        self.assertEqual(client.calls[2][1], client.calls[3][1])
        self.assertEqual(
            client.calls[2][1]["url"],
            "https://example.com/apple-reminders-live-smoke",
        )
        self.assertIn(
            "https://example.com/apple-reminders-live-smoke", client.calls[2][1]["notes"]
        )
        self.assertEqual(client.calls[11][1]["reference"], REF_5)
        cleanup.assert_called_once_with(list_name, LIST_ID)
        for secret in (list_name, LIST_ID, REMINDER_ID, "SOURCE-ID-SECRET", REF_5):
            self.assertNotIn(secret, output.getvalue())

    def test_core_stops_after_uncertain_create_and_attempts_exact_cleanup(self) -> None:
        responses = successful_responses()[:3]
        responses[2][1]["status"] = "committed_verification_pending"
        client = SequenceClient(responses)
        cleanup = mock.Mock(return_value=True)
        output = io.StringIO()
        list_name = "Codex-Apple-Reminders-Live-Smoke-core-pending"

        with self.assertRaises(live_smoke.SmokeFailure):
            live_smoke.run_public_mcp_smoke(
                client,
                source_id="SOURCE-ID-SECRET",
                cleanup=cleanup,
                stdout=output,
                list_name_factory=lambda: list_name,
            )

        self.assertEqual(len(client.calls), 3)
        cleanup.assert_called_once_with(list_name, LIST_ID)
        self.assertIn("step=create_reminder status=failed", output.getvalue())

    def test_core_exact_read_requires_the_note_link_to_survive(self) -> None:
        responses = successful_responses()
        responses[5][1]["data"]["reminder"]["notes"] = (
            "Synthetic public MCP live smoke data."
        )
        client = SequenceClient(responses)
        cleanup = mock.Mock(return_value=True)
        with self.assertRaises(live_smoke.SmokeFailure):
            live_smoke.run_public_mcp_smoke(
                client,
                source_id="SOURCE-ID-SECRET",
                cleanup=cleanup,
                stdout=io.StringIO(),
                list_name_factory=lambda: "Codex-Apple-Reminders-Live-Smoke-core-note",
            )
        self.assertEqual(len(client.calls), 6)
        cleanup.assert_called_once()

    def test_core_exact_read_requires_eventkit_url_metadata_to_survive(self) -> None:
        responses = successful_responses()
        responses[5][1]["data"]["reminder"]["url"] = None
        client = SequenceClient(responses)
        cleanup = mock.Mock(return_value=True)
        output = io.StringIO()

        with self.assertRaises(live_smoke.SmokeFailure):
            live_smoke.run_public_mcp_smoke(
                client,
                source_id="SOURCE-ID-SECRET",
                cleanup=cleanup,
                stdout=output,
                list_name_factory=lambda: "Codex-Apple-Reminders-Live-Smoke-core-url",
            )

        self.assertEqual(len(client.calls), 6)
        self.assertIn("step=exact_read_primary status=failed", output.getvalue())
        cleanup.assert_called_once_with(
            "Codex-Apple-Reminders-Live-Smoke-core-url", LIST_ID
        )


class LiveSmokeWorkflowTests(unittest.TestCase):
    def test_experimental_workflow_rotates_references_and_emits_only_redacted_status(self) -> None:
        client = SequenceClient(successful_responses(experimental=True))
        cleanup = mock.Mock(return_value=True)
        output = io.StringIO()

        live_smoke.run_public_mcp_smoke(
            client,
            source_id="SOURCE-ID-SECRET",
            experimental=True,
            cleanup=cleanup,
            stdout=output,
            list_name_factory=lambda: "Codex-Apple-Reminders-Live-Smoke-testtoken",
        )

        self.assertEqual(client.responses, [])
        self.assertEqual(
            [name for name, _ in client.calls],
            [
                "ensure_reminder_list",
                "ensure_reminder_list",
                "create_reminder_section",
                "create_reminder",
                "create_reminder",
                "fetch_reminders",
                "read_reminder",
                "read_reminder",
                "change_reminder",
                "change_reminder",
                "change_reminder",
                "change_reminder",
                "organize_reminder",
                "change_reminder_attachment",
                "inspect_reminder_native",
                "change_reminder_attachment",
                "inspect_reminder_native",
                "change_reminder_attachment",
                "inspect_reminder_native",
                "delete_reminder",
                "read_reminder",
            ],
        )
        self.assertEqual(client.calls[0][1], client.calls[1][1])
        self.assertEqual(client.calls[3][1], client.calls[4][1])
        fetch_arguments = client.calls[5][1]
        patch_arguments = client.calls[8][1]
        stale_arguments = client.calls[9][1]
        complete_arguments = client.calls[10][1]
        reopen_arguments = client.calls[11][1]
        move_arguments = client.calls[12][1]
        attach_arguments = client.calls[13][1]
        inspect_arguments = client.calls[14][1]
        replace_arguments = client.calls[15][1]
        inspect_replaced_arguments = client.calls[16][1]
        delete_image_arguments = client.calls[17][1]
        inspect_deleted_arguments = client.calls[18][1]
        delete_arguments = client.calls[19][1]
        self.assertEqual(
            fetch_arguments,
            {"list_ids": [LIST_ID], "status": "incomplete", "limit": 5, "sort": "modified"},
        )
        self.assertEqual(patch_arguments["reference"], REF_1)
        self.assertEqual(stale_arguments["reference"], REF_2)
        self.assertEqual(complete_arguments["reference"], REF_3)
        self.assertEqual(reopen_arguments["reference"], REF_4)
        self.assertEqual(move_arguments["reference"], REF_5)
        self.assertEqual(attach_arguments["reference"], REF_6)
        self.assertEqual(inspect_arguments["reference"], REF_7)
        self.assertEqual(replace_arguments["reference"], REF_8)
        self.assertEqual(
            replace_arguments["action"]["attachment_id"], OLD_IMAGE_ID
        )
        self.assertEqual(inspect_replaced_arguments["reference"], REF_9)
        self.assertEqual(delete_image_arguments["reference"], REF_10)
        self.assertEqual(
            delete_image_arguments["action"],
            {"kind": "delete", "attachment_id": NEW_IMAGE_ID},
        )
        self.assertEqual(inspect_deleted_arguments["reference"], REF_11)
        self.assertEqual(delete_arguments["reference"], REF_12)
        self.assertEqual(
            inspect_arguments["include"],
            ["section", "attachments", "sync"],
        )
        self.assertEqual(
            inspect_replaced_arguments["include"],
            ["section", "attachments", "sync"],
        )
        self.assertEqual(
            inspect_deleted_arguments["include"],
            ["section", "attachments", "sync"],
        )
        cleanup.assert_called_once_with(
            "Codex-Apple-Reminders-Live-Smoke-testtoken", LIST_ID
        )

        rendered = output.getvalue()
        for secret in (
            "SOURCE-ID-SECRET",
            LIST_ID,
            SECTION_ID,
            REMINDER_ID,
            REF_1,
            OLD_IMAGE_ID,
            NEW_IMAGE_ID,
            "URL-ATTACHMENT-ID-SECRET",
            "Codex synthetic live smoke reminder",
            "https://example.com/apple-reminders-live-smoke",
            "Codex-Apple-Reminders-Live-Smoke-testtoken",
        ):
            self.assertNotIn(secret, rendered)
        for line in rendered.splitlines():
            self.assertRegex(
                line,
                re.compile(r"^step=[a-z_]+ status=(passed|failed) latency_ms=\d+\.\d{3}$"),
            )

    def test_harness_fails_if_a_stale_parallel_reference_is_still_accepted(self) -> None:
        responses = successful_responses(experimental=True)
        responses[9] = (
            "change_reminder",
            {"ok": True, "status": "verified", "after": {"reference": REF_3}},
        )
        client = SequenceClient(responses)
        cleanup = mock.Mock(return_value=True)
        output = io.StringIO()

        with self.assertRaises(live_smoke.SmokeFailure):
            live_smoke.run_public_mcp_smoke(
                client,
                source_id="SOURCE-ID-SECRET",
                experimental=True,
                cleanup=cleanup,
                stdout=output,
                list_name_factory=lambda: (
                    "Codex-Apple-Reminders-Live-Smoke-staleref"
                ),
            )

        self.assertEqual(
            [name for name, _ in client.calls][-1], "change_reminder"
        )
        self.assertIn(
            "step=stale_reference status=failed latency_ms=",
            output.getvalue(),
        )
        cleanup.assert_called_once_with(
            "Codex-Apple-Reminders-Live-Smoke-staleref", LIST_ID
        )

    def test_native_proof_requires_exact_url_image_and_sync_evidence(self) -> None:
        def remove_sync_evidence(payload):
            data = payload[14][1]["data"]
            data["sync"] = {
                "has_server_record": False,
                "mobile_visible_likely": False,
                "in_cloud": 0,
            }
            for attachment in data["attachments"]:
                attachment["sync"] = {
                    "has_server_record": False,
                    "mobile_visible_likely": False,
                    "in_cloud": 0,
                }

        def make_image_local_only(payload):
            data = payload[14][1]["data"]
            data["sync"] = {"has_server_record": True}
            for attachment in data["attachments"]:
                attachment["sync"] = (
                    {"has_server_record": True}
                    if attachment["type"] == "url"
                    else {"mobile_visible_likely": False}
                )

        mutations = {
            "missing_url": lambda payload: payload[14][1]["data"]["attachments"].pop(0),
            "missing_image": lambda payload: payload[14][1]["data"]["attachments"].pop(),
            "missing_sync": remove_sync_evidence,
            "local_only_image": make_image_local_only,
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                responses = deepcopy(successful_responses(experimental=True))
                mutate(responses)
                cleanup = mock.Mock(return_value=True)
                output = io.StringIO()

                with self.assertRaises(live_smoke.SmokeFailure):
                    live_smoke.run_public_mcp_smoke(
                        SequenceClient(responses),
                        source_id="SOURCE-ID-SECRET",
                        experimental=True,
                        cleanup=cleanup,
                        stdout=output,
                        list_name_factory=lambda: (
                            "Codex-Apple-Reminders-Live-Smoke-nativeproof"
                        ),
                    )

                self.assertIn(
                    "step=inspect_native status=failed latency_ms=",
                    output.getvalue(),
                )
                cleanup.assert_called_once_with(
                    "Codex-Apple-Reminders-Live-Smoke-nativeproof", LIST_ID
                )

    def test_harness_rejects_replacement_that_keeps_the_old_image(self) -> None:
        responses = deepcopy(successful_responses(experimental=True))
        responses[15][1]["after"]["attachments"].append(
            {"id": OLD_IMAGE_ID, "type": "image"}
        )
        output = io.StringIO()

        with self.assertRaises(live_smoke.SmokeFailure):
            live_smoke.run_public_mcp_smoke(
                SequenceClient(responses),
                source_id="SOURCE-ID-SECRET",
                experimental=True,
                cleanup=mock.Mock(return_value=True),
                stdout=output,
                list_name_factory=lambda: (
                    "Codex-Apple-Reminders-Live-Smoke-replaceduplicate"
                ),
            )

        self.assertIn(
            "step=replace_image status=failed latency_ms=",
            output.getvalue(),
        )

    def test_harness_rejects_delete_that_keeps_an_image(self) -> None:
        responses = deepcopy(successful_responses(experimental=True))
        responses[17][1]["after"]["attachments"].append(
            {"id": NEW_IMAGE_ID, "type": "image"}
        )
        output = io.StringIO()

        with self.assertRaises(live_smoke.SmokeFailure):
            live_smoke.run_public_mcp_smoke(
                SequenceClient(responses),
                source_id="SOURCE-ID-SECRET",
                experimental=True,
                cleanup=mock.Mock(return_value=True),
                stdout=output,
                list_name_factory=lambda: (
                    "Codex-Apple-Reminders-Live-Smoke-deleteimage"
                ),
            )

        self.assertIn(
            "step=delete_image status=failed latency_ms=",
            output.getvalue(),
        )

    def test_native_rechecks_replacement_and_delete_state(self) -> None:
        def keep_old_image(responses):
            responses[16][1]["data"]["attachments"].append(
                {
                    "id": OLD_IMAGE_ID,
                    "type": "image",
                    "sync": {"mobile_visible_likely": True},
                }
            )

        def lose_mobile_evidence(responses):
            responses[16][1]["data"]["attachments"][1]["sync"] = {
                "mobile_visible_likely": False
            }

        def keep_deleted_image(responses):
            responses[18][1]["data"]["attachments"].append(
                {
                    "id": NEW_IMAGE_ID,
                    "type": "image",
                    "sync": {"mobile_visible_likely": True},
                }
            )

        cases = {
            "replacement_keeps_old": (keep_old_image, "inspect_replaced_image"),
            "replacement_loses_mobile_evidence": (
                lose_mobile_evidence,
                "inspect_replaced_image",
            ),
            "delete_keeps_image": (keep_deleted_image, "inspect_deleted_image"),
        }
        for label, (mutate, failed_step) in cases.items():
            with self.subTest(label=label):
                responses = deepcopy(successful_responses(experimental=True))
                mutate(responses)
                output = io.StringIO()

                with self.assertRaises(live_smoke.SmokeFailure):
                    live_smoke.run_public_mcp_smoke(
                        SequenceClient(responses),
                        source_id="SOURCE-ID-SECRET",
                        experimental=True,
                        cleanup=mock.Mock(return_value=True),
                        stdout=output,
                        list_name_factory=lambda: (
                            "Codex-Apple-Reminders-Live-Smoke-native-recheck"
                        ),
                    )

                self.assertIn(
                    f"step={failed_step} status=failed latency_ms=",
                    output.getvalue(),
                )

    def test_failure_still_attempts_cleanup_and_names_only_unproven_residue(self) -> None:
        list_name = "Codex-Apple-Reminders-Live-Smoke-needsmanualcleanup"
        client = SequenceClient(
            [
                successful_responses(experimental=True)[0],
                successful_responses(experimental=True)[1],
                (
                    "create_reminder_section",
                    {
                        "ok": True,
                        "status": "committed_verification_pending",
                        "target": {"section_id": SECTION_ID},
                    },
                ),
            ]
        )
        cleanup = mock.Mock(return_value=False)
        output = io.StringIO()

        with self.assertRaises(live_smoke.SmokeFailure):
            live_smoke.run_public_mcp_smoke(
                client,
                source_id="SOURCE-ID-SECRET",
                experimental=True,
                cleanup=cleanup,
                stdout=output,
                list_name_factory=lambda: list_name,
            )

        cleanup.assert_called_once_with(list_name, LIST_ID)
        rendered = output.getvalue()
        self.assertIn(
            "step=create_section status=failed latency_ms=", rendered
        )
        self.assertRegex(
            rendered,
            rf"step=cleanup:{re.escape(list_name)} status=failed latency_ms=\d+\.\d{{3}}",
        )
        for secret in ("SOURCE-ID-SECRET", LIST_ID, SECTION_ID):
            self.assertNotIn(secret, rendered)


class ExactListCleanupTests(unittest.TestCase):
    def test_cleanup_passes_name_and_id_only_as_osascript_argv(self) -> None:
        list_name = "Codex-Apple-Reminders-Live-Smoke-cleanup"
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deleted\n", stderr=""
            )
        )

        self.assertTrue(
            live_smoke.cleanup_synthetic_list(
                list_name, LIST_ID, runner=runner
            )
        )

        command = runner.call_args.args[0]
        self.assertEqual(command[-2:], [list_name, LIST_ID])
        self.assertEqual(command[:3], ["/usr/bin/osascript", "-e", live_smoke.CLEANUP_SCRIPT])
        self.assertNotIn(list_name, live_smoke.CLEANUP_SCRIPT)
        self.assertNotIn(LIST_ID, live_smoke.CLEANUP_SCRIPT)
        self.assertIn("(count of namedLists) is not 1", live_smoke.CLEANUP_SCRIPT)
        self.assertIn("resolvedID is not expectedID", live_smoke.CLEANUP_SCRIPT)

    def test_cleanup_refuses_unreserved_names_and_unproven_results(self) -> None:
        runner = mock.Mock()
        self.assertFalse(
            live_smoke.cleanup_synthetic_list(
                "Personal", LIST_ID, runner=runner
            )
        )
        runner.assert_not_called()

        runner.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="id_mismatch\n", stderr=""
        )
        self.assertFalse(
            live_smoke.cleanup_synthetic_list(
                "Codex-Apple-Reminders-Live-Smoke-mismatch",
                LIST_ID,
                runner=runner,
            )
        )


if __name__ == "__main__":
    unittest.main()
